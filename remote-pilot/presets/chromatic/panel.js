// SPDX-License-Identifier: GPL-3.0-or-later
// Additional terms (author attribution, GPLv3 section 7(b)): see ADDITIONAL-TERMS.md
// Chromatic preset UI. Loaded by the shared viewer from the manifest's panelScript.
// It builds its own device shell and panels and acts only while
// body[data-preset="chromatic"]; theme.css hides the dual-screen parts meanwhile.
// Pure helpers are exported on globalThis.RemotePilotChromatic for tests.
(() => {
  'use strict';
  const ID = 'chromatic';
  const BUTTONS = ['UP', 'DOWN', 'LEFT', 'RIGHT', 'A', 'B', 'START', 'SELECT', 'MENU'];
  const STATES = {
    available: ['Available', 'good'], waiting: ['Waiting', 'wait'], off: ['Off', 'off'],
    'needs-launcher': ['Needs launcher work', 'launcher'], 'needs-fpga': ['Needs FPGA update', 'fpga'],
    manual: ['Run the Manager yourself', 'manual'], 'n/a': ['Not applicable', 'off'],
  };
  const GROUPS = {connection: 'Connection', launcher: 'Launcher (ESP32 overlay)', game: 'Games (stock FPGA)',
    captures: 'Captures and diagnostics', firmware: 'Firmware', 'not-applicable': 'Not applicable'};
  const MODE_LABELS = {
    off: 'Off', lan: 'Wi-Fi (paired console)', serial: 'USB serial (read only)',
    usb: 'USB (CHR1)', auto: 'Automatic: USB, else Wi-Fi',
    'lan-sim': 'Simulated launcher over Wi-Fi', 'lan-sim-fpga': 'Simulated future device (FPGA game bridge)',
    'serial-sim': 'Simulated USB serial (read only)', 'usb-sim': 'Simulated USB console (pairing demo)',
    'auto-sim': 'Simulated console on USB and Wi-Fi',
  };
  const SIM_MODES = ['off', 'serial-sim', 'lan-sim', 'lan-sim-fpga', 'usb-sim', 'auto-sim'];

  // ---- plain-language tooltips: what each part is, what it is for, when
  // to use it, in everyday words. The shared viewer shows data-tip on hover and keyboard focus.
  const CHR1 = 'CHR1 is the private language Remote Pilot and the launcher speak once paired';
  const BUTTON_TIPS = {
    UP: 'Up on the D-pad (the cross-shaped pad). Click or hold it to press Up on the console, for moving through menus and games.',
    DOWN: 'Down on the D-pad. Click or hold it to press Down on the console.',
    LEFT: 'Left on the D-pad. Click or hold it to press Left on the console.',
    RIGHT: 'Right on the D-pad. Click or hold it to press Right on the console.',
    A: 'The A button, usually "OK" or "jump". Click or hold it to press A on the console.',
    B: 'The B button, usually "back" or "cancel". Click or hold it to press B on the console.',
    START: 'Start, usually starts or pauses a game. Click or hold it to press Start on the console.',
    SELECT: 'Select, usually switches an option in a game. Click or hold it to press Select on the console.',
    MENU: 'The Menu button on the console\'s right side. One click closes the launcher menu and hands the console over to its normal (stock) software, just like pressing it for real.',
  };
  const SIDE_TIPS = [  // same order as SIDE
    'The power switch on the top edge. Slide it on the real console to turn it on or off; it cannot be switched from this PC.',
    'The volume wheel on the right side. Turn it on the real console to make it louder or quieter; it is not controlled from here.',
    'The link cable port on the left side, for joining two consoles with a link cable. Shown for reference only.',
    'The USB-C port on the bottom edge, for charging and for the cable to this PC. With the cable in, Remote Pilot can reach the console without Wi-Fi.',
    'The headphone jack on the bottom edge, for wired headphones. Shown for reference only.',
  ];
  const STATE_TIPS = {
    available: 'Available: this works right now.',
    waiting: 'Waiting: this should work, but the console or the connection is not ready yet. The small text says what it is waiting for.',
    off: 'Off: not in use with the current connection setting.',
    'needs-launcher': 'Needs launcher work: the launcher (the small menu program on the console) needs an update before this can work.',
    'needs-fpga': 'Needs FPGA update: games run on the console\'s game chip (the FPGA), and its factory setup does not share pictures, sound or buttons with a PC yet.',
    manual: 'Run the Manager yourself: do this step in the Chromatic Manager app, with the console in front of you.',
    'n/a': 'Not applicable: this console does not have this feature.',
  };
  const GROUP_TIPS = {
    connection: 'Ways this PC can reach the console: a USB cable or your home Wi-Fi.',
    launcher: 'Things that work while the launcher menu is on screen. The launcher runs on the console\'s built-in Wi-Fi chip (the ESP32) before a game starts.',
    game: 'Things that work while a game runs. Games run on the console\'s game chip (the FPGA); its unchanged factory (stock) setup does not share the game with a PC yet.',
    captures: 'Saving pictures, logs and reports on this PC.',
    firmware: 'Installing or checking the console\'s own software, always through the Chromatic Manager app.',
    'not-applicable': 'Features this console does not have.',
  };
  const FEATURE_TIPS = {
    'usb-link': 'Talking to the console over a USB cable. Needs a paired console with launcher 1.1.2 or newer.',
    'wifi-link': 'Talking to the console over your home Wi-Fi, without a cable. It never leaves your local network.',
    pairing: 'The secret key that lets this PC and your console trust each other. It is kept privately on this PC and never shown.',
    'usb-serial': 'A read-only way to see the launcher\'s messages over USB. It cannot press buttons or take pictures.',
    'launcher-status': 'Basic news from the launcher: that it started, its version, and when it hands over to a game.',
    'launcher-ping': 'A quick "are you there?" check that measures how long the console takes to answer.',
    'launcher-log': 'Fetching the launcher\'s log, a running diary of what it has done, to save on this PC.',
    'launcher-screen': 'Taking screenshots while the launcher menu is on screen.',
    'launcher-video': 'Watching the launcher menu live. CHV1 is the name of its video format.',
    'launcher-input': 'Pressing launcher menu buttons from this PC, one at a time or several together.',
    'launcher-hold': 'Holding buttons down for as long as you like, and using a game controller plugged into this PC.',
    'launcher-console': 'Asking the console simple questions, such as its version or free memory. It cannot change anything.',
    'launcher-perf': 'Timing numbers for the graph: how quickly the console draws its screen.',
    'game-screen': 'Screenshots while a game is running.',
    'game-video': 'Watching a game live on this PC.',
    'game-input': 'Pressing buttons in a running game from this PC.',
    'game-audio': 'Hearing a game\'s sound on this PC.',
    'capture-bundle': 'Saving a screenshot, the log and a list of the files together in one go.',
    report: 'Saving a short written report of how everything stands right now.',
    diagnostics: 'Saving one troubleshooting item, such as the device log or a status snapshot.',
    'manager-status': 'Reading the Chromatic Manager\'s files to show versions and backups. Nothing on the console changes.',
    'firmware-patch': 'Running the Chromatic Manager from this page to install, remove or restore the console\'s software, with a backup first.',
    'ota-launcher': 'A planned way to update the launcher over Wi-Fi. Only designed so far; it would never touch the console\'s factory software.',
  };
  const FACT_TIPS = {
    Connection: 'How this PC is reaching the console: USB, Wi-Fi or a simulator.',
    Launcher: 'The version of the launcher, the small menu program on the console that starts games or media.',
    Phase: 'What the console is doing now: launcher menu, game, factory (stock) software, or starting up.',
    'Screen owner': 'Who is drawing the screen at the moment: the launcher or a game.',
    Held: 'Which buttons this PC is holding down right now.',
    'Last reset': 'Why the console last restarted, in the console\'s own words.',
    'Lines received': 'How many status messages the console has sent this PC so far.',
    'Last contact': 'How long ago the console last sent anything. A growing number can mean the link dropped.',
    'Link in use': 'Whether the live connection is using the USB cable or Wi-Fi right now.',
    USB: 'The state of the USB cable connection, in the console\'s own short words.',
    'Session lease': 'Time left on the current connection permission. It renews by itself while the link is healthy.',
    Key: 'Which kind of pairing key this connection uses.',
    Address: 'The console\'s address on your home network (for a simulator, this PC).',
    Note: 'The latest warning or error about the connection.',
  };
  const CONSOLE_TIPS = {
    help: '"help" lists the questions the console understands.',
    version: '"version" shows which launcher version is installed.',
    uptime: '"uptime" shows how long the console has been switched on.',
    heap: '"heap" shows how much free memory the console has.',
    wifi: '"wifi" shows the console\'s Wi-Fi connection status.',
    phase: '"phase" shows what the console is doing: launcher menu, game or factory software.',
    'reset-reason': '"reset-reason" shows why the console last restarted.',
  };
  const MANAGER_TIPS = {
    status: '"status" only reads and reports the console\'s software; nothing is written.',
    backup: '"backup" saves a full copy of the console\'s software on this PC; nothing on the console changes.',
    install: '"install" puts the launcher menu on the console, after making a backup.',
    remove: '"remove" takes the launcher off and leaves the console\'s factory software.',
    'restore-stock': '"restore-stock" puts the console\'s original factory (stock) software back from a backup.',
  };
  function choiceTip(base, table, value) {  // a drop-down's tooltip: what it is, then the current choice
    return table[value] ? `${base} ${table[value]}` : base;
  }
  // Standard Gamepad API buttons -> Chromatic. MENU is never mapped: it hands over to stock.
  const PAD_MAP = {0: 'A', 1: 'B', 2: 'B', 3: 'A', 8: 'SELECT', 9: 'START', 12: 'UP', 13: 'DOWN', 14: 'LEFT', 15: 'RIGHT'};

  function badge(state) {
    const [text, tone] = STATES[state] || ['Unknown', 'off'];
    return {text, tone};
  }

  function feature(status, id) {
    return (status?.features || []).find(item => item.id === id) || {state: 'off', detail: 'No status yet'};
  }

  // Whether a control may be used, and the honest reason when not. Device controls
  // address whatever owns the screen: the launcher, or a game when the FPGA allows.
  function controlState(status, kind) {
    if (!status) return {enabled: false, reason: 'Waiting for the receiver'};
    if (!status.selected) return {enabled: false, reason: 'Select the Chromatic preset'};
    if (kind === 'log') {
      const lines = status.transport?.lines || status.tail?.length || 0;
      return status.mode === 'off' ? {enabled: false, reason: 'Chromatic link is off'} :
        lines ? {enabled: true, reason: ''} : {enabled: false, reason: 'No device lines received yet'};
    }
    const game = status.transport?.target === 'game';
    const ids = {input: ['launcher-input', 'game-input'], hold: ['launcher-hold', 'game-input'],
      shot: ['launcher-screen', 'game-screen'], video: ['launcher-video', 'game-video'],
      audio: [null, 'game-audio'], console: ['launcher-console', 'launcher-console']}[kind];
    const id = ids[game ? 1 : 0] || ids[1];
    const item = feature(status, id);
    if (item.state !== 'available') return {enabled: false, reason: item.detail || badge(item.state).text};
    if (!status.linkEnabled) return {enabled: false, reason: 'PC link disconnected; press Connect'};
    if ((kind === 'input' || kind === 'hold') && !status.controlsArmed)
      return {enabled: false, reason: 'Enable remote controls in this tab first'};
    return {enabled: true, reason: ''};
  }

  function connectText(status) {
    if (!status) return 'Connecting to the local receiver.';
    const t = status.transport || {};
    const sim = t.simulated ? 'SIMULATOR — no console involved. ' : '';
    if (status.mode === 'off') {
      const offer = status.usbOffer;
      if (offer?.message) return `Chromatic link is off. ${offer.message}`;
      return 'Chromatic link is off. Press Connect to use the console: USB first (the port opens only after you allow it), then paired Wi-Fi. Or try a simulator in Chromatic tools.';
    }
    if (t.usbState === 'no-pyserial' || t.alternate?.usbState === 'no-pyserial')
      return `${sim}USB support (pyserial) is not installed in this Python. Start Remote Pilot with Start-RemotePilot.cmd, or run pip install pyserial.`;
    const pairing = t.pairing || status.usbPairing;
    if (pairing?.state === 'waiting') return `${sim}Pairing code ${pairing.code}: check the console shows the same code, then press A on the console (B refuses). With no cartridge the screen stays dark: press A anyway.`;
    if (t.phase === 'launcher') return `${sim}Launcher ${t.launcherVersion || ''} is running (${t.label})${t.session ? ', remote session open' : ''}.`;
    if (t.phase === 'game') return `${sim}A game is running and the device offers game access (${t.label}).`;
    if (t.phase === 'stock') return `${sim}The launcher handed over to stock firmware. Games run on the unmodified stock FPGA, which this viewer cannot see or drive until an FPGA update exists.`;
    if (t.phase === 'booting') return `${sim}The console is starting.`;
    return `${sim}${t.label || 'Transport'}: ${t.error || status.scan || 'waiting for the console'}.`;
  }

  // "Allow USB on COMx": the console's port was only listed (USB vendor ID 374E), never opened.
  // Only this PC's own browser may allow it; the click writes local/chromatic/config.json.
  function usbOfferView(status) {
    const offer = status?.usbOffer;
    const ports = offer?.state === 'found' ? [...(offer.ports || [])] : [];
    // Without a port to allow, the connection text above already says why (no console, no pyserial).
    if (!ports.length || !status.selected) return {visible: false, ports: [], canAllow: false, text: ''};
    const canAllow = ports.length > 0 && Boolean(status.localClient) && status.linkEnabled !== false;
    const reason = !status.localClient ? ' Allowing the port works only from this PC\'s own browser.' :
      status.linkEnabled === false ? ' Press Connect first.' :
      ' The click lets Remote Pilot open the port (DTR and RTS stay low; only CHR1 lines are written) and saves that in local/chromatic/config.json, which Git ignores.';
    return {visible: true, ports, canAllow, text: reason.trim()};
  }

  // The USB hand-off sign: amber while Chromatic Manager holds the console's USB cable (its
  // jobs need the one USB port), green for a few seconds once it is back. Wi-Fi keeps working.
  function usbHoldView(status, wasPaused = false) {
    const hold = status?.usbHold;
    if (hold) {
      const who = hold.unreadable ? 'Another program' : (hold.holder || 'Chromatic Manager');
      const what = hold.reason ? ` (${hold.reason})` : '';
      const gone = hold.holderRunning === false || Boolean(hold.unreadable);
      return {visible: true, state: 'paused', title: 'USB paused',
        text: gone ? `${who} closed without giving the USB cable back. Wi-Fi still works. If no job was cut off, press Use USB again; if one was, open Chromatic Manager and press Finish it.` :
          `${who} is using the USB cable${what}. Wi-Fi keeps working; USB is back in a moment.`,
        canTake: gone && Boolean(status.localClient)};
    }
    if (wasPaused) return {visible: true, state: 'back', title: 'USB back', text: 'Chromatic Manager has finished. The USB link works again.', canTake: false};
    return {visible: false, state: '', title: '', text: '', canTake: false};
  }

  function modeOptions(status) {
    const configured = status?.configuredMode || 'off';
    return [['off', 'Off'], ['lan-sim', MODE_LABELS['lan-sim']], ['lan-sim-fpga', MODE_LABELS['lan-sim-fpga']],
      ['serial-sim', MODE_LABELS['serial-sim']], ['usb-sim', MODE_LABELS['usb-sim']], ['auto-sim', MODE_LABELS['auto-sim']],
      ['configured', `Configured: ${MODE_LABELS[configured] || configured}`]]
      .map(([value, label]) => ({value, label}));
  }

  // The USB pairing box: what to show and whether "Pair over USB" may be pressed.
  function pairingView(status) {
    const t = status?.transport || {};
    const usb = t.kind === 'usb' || t.usbState !== undefined && t.usbState !== null;
    const state = t.pairing || status?.usbPairing || null;
    if (!usb) return {visible: false, canStart: false, text: ''};
    if (state?.state === 'waiting') return {visible: true, canStart: false, code: state.code,
      text: `Code ${state.code} · ${state.secondsLeft ?? '?'} s left. Press A on the console if it shows the same code; B refuses.`};
    if (state?.state === 'asking') return {visible: true, canStart: false, text: 'Asking the console…'};
    const ready = !['stock', 'old-launcher', 'unplugged', 'closed', 'no-pyserial'].includes(t.usbState);
    const offer = ready && (t.usbState === 'nokey' || t.usbState === 'denied' || t.pcHasKey === false && !t.session ||
      state && state.state !== 'paired');
    return {visible: offer || Boolean(state), canStart: Boolean(offer) && status?.linkEnabled !== false,
      text: state?.message || 'The console has no key for this PC yet. Pairing shows a code on both screens; only an A press on the console approves.'};
  }

  function padButtons(pad) {
    const names = new Set();
    (pad?.buttons || []).forEach((button, index) => {
      if ((button?.pressed || button?.value > 0.5) && PAD_MAP[index]) names.add(PAD_MAP[index]);
    });
    const [x = 0, y = 0] = pad?.axes || [];
    if (x < -0.5) names.add('LEFT'); if (x > 0.5) names.add('RIGHT');
    if (y < -0.5) names.add('UP'); if (y > 0.5) names.add('DOWN');
    return BUTTONS.filter(name => names.has(name)).slice(0, 4);
  }

  function chordPayload(names, ms = 100) {
    const list = BUTTONS.filter(name => names.includes(name));
    if (!list.length || list.length > 4) throw new Error('Choose 1 to 4 buttons');
    if (list.includes('MENU') && list.length > 1) throw new Error('MENU is never part of a chord');
    return {buttons: list, ms};
  }

  // ---- device drawing: a green Chromatic, cell by cell ---------------------------
  // Pixel style of the Chromatic Manager's console art (chromatic_theme.draw_console), with the
  // real layout of the console. The shell is 60 x 100 cells; the view adds 3 cells on
  // the left, 7 on the right and 2 above and below for the edge controls and their names.
  // Colours are CSS classes (.chr-k-*) whose values sit in theme.css's DEVICE COLOURS block.
  // No ModRetro wordmark is drawn: the name area under the screen stays plain.
  const VIEW = {x: -3, y: -2, w: 70, h: 104};
  const PARTS = {  // [x, y, w, h] in shell cells
    screen: [10, 9, 40, 36], strip: [10, 45.7, 40, 3.1],
    UP: [13, 57, 6, 6], DOWN: [13, 69, 6, 6], LEFT: [7, 63, 6, 6], RIGHT: [19, 63, 6, 6],
    A: [48, 60, 8, 8], B: [38, 64, 8, 8], SELECT: [20, 79, 10, 7], START: [32, 79, 10, 7],
    MENU: [60, 47, 2, 6],  // right side, level with the bottom of the screen bezel
  };
  const SIDE = [  // edge controls seen from the front: [title, x, y, w, h]
    ['Power switch (top edge, left)', 5, -2, 8, 2], ['Volume wheel (right side)', 60, 39, 2, 8],
    ['Link cable port (left side)', 0, 41, 2, 10], ['USB-C port (bottom edge)', 26, 98, 7, 2],
    ['Headphone jack (bottom edge)', 39, 98, 3, 2]];

  function inRounded(x, y, x0, y0, w, h, [tl, tr, br, bl]) {
    if (x < x0 || y < y0 || x >= x0 + w || y >= y0 + h) return false;
    for (const [r, cx, cy, inside] of [[tl, x0 + tl, y0 + tl, x < x0 + tl && y < y0 + tl],
      [tr, x0 + w - tr, y0 + tr, x >= x0 + w - tr && y < y0 + tr],
      [br, x0 + w - br, y0 + h - br, x >= x0 + w - br && y >= y0 + h - br],
      [bl, x0 + bl, y0 + h - bl, x < x0 + bl && y >= y0 + h - bl]])
      if (inside && (x + 0.5 - cx) ** 2 + (y + 0.5 - cy) ** 2 > r * r) return false;
    return true;
  }
  // Cells -> SVG rects, one run per row and colour.
  function cellsToRects(grid, x0, y0, w, h, extra = '') {
    const out = [];
    for (let y = 0; y < h; y++) {
      for (let x = 0; x < w;) {
        const key = grid[y * w + x];
        let end = x + 1;
        while (end < w && grid[y * w + end] === key) end++;
        if (key) out.push(`<rect x="${x + x0}" y="${y + y0}" width="${end - x}" height="1" class="${key.split(' ').map(k => `chr-k-${k}`).join(' ')}${extra}"/>`);
        x = end;
      }
    }
    return out.join('');
  }
  function pillShape(x, y) {  // a Select/Start pill angled like the real one, in a 10 x 7 box
    const [ax, ay, bx, by] = [1.8, 4.9, 8.2, 2.1], px = x + 0.5, py = y + 0.5;
    const t = Math.max(0, Math.min(1, ((px - ax) * (bx - ax) + (py - ay) * (by - ay)) / ((bx - ax) ** 2 + (by - ay) ** 2)));
    return (px - ax - t * (bx - ax)) ** 2 + (py - ay - t * (by - ay)) ** 2 <= 1.75 ** 2;
  }
  const discShape = (x, y) => (x + 0.5 - 4) ** 2 + (y + 0.5 - 4) ** 2 <= 16.4;

  // One button's own pixel art (lights up while held: its "face" cells take --dev-pressed).
  function buttonArt(name) {
    const [, , w, h] = PARTS[name];
    const grid = new Array(w * h).fill('');
    let inShape, face = 'btn', hi = 'btn-hi', lo = 'btn-lo';
    const outer = {UP: 'tlr', DOWN: 'blr', LEFT: 'ltb', RIGHT: 'rtb', MENU: 'r'}[name];
    if (outer) {  // D-pad arms and the Menu nub: shaded only on their outer sides
      const cut = {UP: [[0, 0], [5, 0]], DOWN: [[0, 5], [5, 5]], LEFT: [[0, 0], [0, 5]], RIGHT: [[5, 0], [5, 5]],
        MENU: [[1, 0], [1, h - 1]]}[name];
      inShape = (x, y) => !cut.some(([cx, cy]) => cx === x && cy === y);
    } else if (name === 'A' || name === 'B') {
      inShape = discShape; face = 'ab'; hi = 'ab-hi'; lo = 'ab-lo';
    } else {  // SELECT, START
      inShape = pillShape;
    }
    const has = (x, y) => x >= 0 && y >= 0 && x < w && y < h && inShape(x, y);
    for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
      if (!has(x, y)) continue;
      const top = !has(x, y - 1) && (!outer || outer.includes('t')), left = !has(x - 1, y) && (!outer || outer.includes('l'));
      const bottom = !has(x, y + 1) && (!outer || outer.includes('b')), right = !has(x + 1, y) && (!outer || outer.includes('r'));
      grid[y * w + x] = bottom || right ? lo : top || left ? hi : `${face} face`;
    }
    if (face === 'ab') grid[2 * w + 2] = 'ab-hi';  // a glint
    return `<svg viewBox="0 0 ${w} ${h}" shape-rendering="crispEdges" aria-hidden="true" focusable="false">${cellsToRects(grid, 0, 0, w, h)}</svg>`;
  }

  // The shell and everything printed on it, plus where each live part sits (percent of the view).
  function deviceArt() {
    const {x: vx, y: vy, w: vw, h: vh} = VIEW;
    const grid = new Array(vw * vh).fill('');
    const set = (x, y, key) => { if (x >= vx && y >= vy && x < vx + vw && y < vy + vh) grid[(y - vy) * vw + (x - vx)] = key; };
    const get = (x, y) => (x >= vx && y >= vy && x < vx + vw && y < vy + vh) ? grid[(y - vy) * vw + (x - vx)] : '';
    const shell = (x, y) => inRounded(x, y, 0, 0, 60, 100, [3, 3, 6, 4]);
    const rect = (x0, y0, w, h, key) => { for (let y = y0; y < y0 + h; y++) for (let x = x0; x < x0 + w; x++) set(x, y, key); };
    // shell with a dark outline, a lit top/left rim and a shaded bottom/right rim
    for (let y = 0; y < 100; y++) for (let x = 0; x < 60; x++) {
      if (!shell(x, y)) continue;
      const edge = !shell(x - 1, y) || !shell(x + 1, y) || !shell(x, y - 1) || !shell(x, y + 1);
      set(x, y, edge ? 'edge' : 'body');
    }
    for (let y = 0; y < 100; y++) for (let x = 0; x < 60; x++) {
      if (get(x, y) !== 'body') continue;
      if (get(x + 1, y) === 'edge' || get(x, y + 1) === 'edge') set(x, y, 'body-lo');
      else if (get(x - 1, y) === 'edge' || get(x, y - 1) === 'edge') set(x, y, 'body-hi');
      // accent paint: diagonal stripes in the bottom-left corner, white, mint, teal, dark teal
      const d = x + (99 - y);
      if (d < 21) set(x, y, d < 9 ? 'stripe-1' : d < 13 ? 'stripe-2' : d < 17 ? 'stripe-3' : 'stripe-4');
    }
    // charcoal glass panel with the larger curve at its bottom-right corner, and the screen
    const bezel = (x, y) => inRounded(x, y, 4, 4, 52, 46, [2, 2, 11, 2]);
    for (let y = 4; y < 50; y++) for (let x = 4; x < 56; x++) {
      if (!bezel(x, y)) continue;
      const edge = !bezel(x - 1, y) || !bezel(x + 1, y) || !bezel(x, y - 1) || !bezel(x, y + 1);
      set(x, y, edge ? 'bezel-edge' : y === 5 ? 'bezel-hi' : 'bezel');
    }
    rect(9, 8, 42, 38, 'lcd');
    // printed strip under the screen: small darker squares growing into taller bars at the right
    for (let i = 0; i < 8; i++) rect(27 + i * 3, 52, 2, 2, 'print');
    rect(51, 50, 2, 4, 'print'); rect(54, 46, 2, 8, 'print');
    // recesses round the D-pad, A/B and under Select/Start, then the D-pad hub
    rect(6, 62, 20, 8, 'body-lo'); rect(12, 56, 8, 20, 'body-lo');
    // the whole cross in button colour under the four arm buttons, so the hairline seams the browser
    // leaves between separately placed parts show D-pad black, never the shell
    rect(13, 57, 6, 18, 'btn'); rect(7, 63, 18, 6, 'btn');
    set(15, 65, 'btn-hi'); set(16, 66, 'btn-lo');
    for (const [cx, cy] of [[52, 64], [42, 68]])
      for (let y = cy - 5; y <= cy + 5; y++) for (let x = cx - 5; x <= cx + 5; x++)
        if ((x + 0.5 - cx) ** 2 + (y + 0.5 - cy) ** 2 <= 4.9 ** 2) set(x, y, 'body-lo');
    for (const x0 of [20, 32]) for (let y = 0; y < 7; y++) for (let x = 0; x < 10; x++) if (pillShape(x, y)) set(x0 + x, 80 + y, 'body-lo');
    // edge controls, seen from the front: power switch on the top edge (left), volume wheel on the
    // right side above the Menu button, link port on the left side, USB-C and headphones below
    rect(5, -1, 8, 1, 'port'); rect(6, -2, 3, 2, 'metal');
    for (let y = 39; y < 47; y++) { set(60, y, y % 2 ? 'wheel' : 'wheel-hi'); if (y > 39 && y < 46) set(61, y, y % 2 ? 'wheel-hi' : 'wheel'); }
    rect(0, 41, 1, 10, 'port'); rect(1, 42, 1, 8, 'port'); rect(1, 44, 1, 4, 'metal');
    rect(26, 99, 7, 1, 'port'); rect(27, 98, 5, 1, 'port'); rect(28, 99, 3, 1, 'metal');
    rect(39, 98, 3, 2, 'port'); set(40, 99, 'btn-lo');
    const text = (label, x, y, size, cls, rotate = 0) => `<text x="${x}" y="${y}" font-size="${size}" text-anchor="middle" class="chr-k-${cls}"` +
      `${rotate ? ` transform="rotate(${rotate} ${x} ${y})"` : ''}>${label}</text>`;
    const labels = [text('A', 54.6, 71.6, 2.4, 'label'), text('B', 44.4, 75.8, 2.4, 'label'),
      text('SELECT', 24.6, 89.4, 1.9, 'label', -25), text('START', 36.6, 89.4, 1.9, 'label', -25),
      text('POWER', 17.6, -0.4, 1.7, 'side'), text('VOL', 64.4, 43, 1.7, 'side', 90), text('MENU', 64.4, 50, 1.7, 'side', 90),
      text('LINK', -1.6, 46, 1.7, 'side', -90), text('USB-C', 29.5, 101.7, 1.7, 'side'), text('AUDIO', 40.5, 101.7, 1.7, 'side')].join('');
    const svg = `<svg class="chr-art" viewBox="${vx} ${vy} ${vw} ${vh}" shape-rendering="crispEdges" aria-hidden="true" focusable="false">` +
      `${cellsToRects(grid, vx, vy, vw, vh)}${labels}</svg>`;
    const place = ([x, y, w, h]) => ({left: (x - vx) / vw * 100, top: (y - vy) / vh * 100, width: w / vw * 100, height: h / vh * 100});
    // The edge controls get hover/focus spots (one cell larger all round, kept inside the view)
    // that carry their tooltips; the drawing itself stays aria-hidden.
    const side = SIDE.map(([title, x, y, w, h]) => {
      const x0 = Math.max(vx, x - 1), y0 = Math.max(vy, y - 1);
      return {title, ...place([x0, y0, Math.min(vx + vw, x + w + 1) - x0, Math.min(vy + vh, y + h + 1) - y0])};
    });
    return {svg, view: VIEW, side, parts: Object.fromEntries(Object.entries(PARTS).map(([name, box]) => [name, place(box)]))};
  }

  globalThis.RemotePilotChromatic = {badge, controlState, connectText, modeOptions, pairingView, usbOfferView, usbHoldView, padButtons, chordPayload,
    deviceArt, buttonArt, BUTTONS, PAD_MAP, PARTS, SIDE,
    BUTTON_TIPS, SIDE_TIPS, STATE_TIPS, GROUP_TIPS, FEATURE_TIPS, FACT_TIPS, CONSOLE_TIPS, MANAGER_TIPS, STATES, GROUPS, choiceTip};
  if (typeof document === 'undefined') return;

  // ---- DOM ------------------------------------------------------------------------
  const $ = id => document.getElementById(id);
  function el(tag, props = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props)) {
      if (key === 'class') node.className = value;
      else if (key === 'text') node.textContent = value;
      else if (key.startsWith('data-') || key.startsWith('aria-')) node.setAttribute(key, value);
      else node[key] = value;
    }
    node.append(...children);
    return node;
  }
  const owner = Array.from(crypto.getRandomValues(new Uint8Array(16)), n => n.toString(16).padStart(2, '0')).join('');
  let status = null, timer = null, liveTimer = null, armTimer = null, holdTimer = null, padTimer = null, soundTimer = null;
  let built = false, frameNumber = 0, liveTimes = [], liveBusy = false;
  const pointerHeld = new Set(), chord = new Set();
  let padHeld = [], sentHold = '', audioContext = null, audioNext = 0;
  let holdShown = false, holdBackUntil = 0;

  async function post(action, body = {}) {
    const response = await fetch(`/api/chromatic/${action}`, {method: 'POST', cache: 'no-store',
      headers: {'Content-Type': 'application/json', 'X-Remote-Pilot-Viewer': '1'}, body: JSON.stringify(body)});
    const text = await response.text();
    if (!response.ok) throw new Error(text || `Request failed (${response.status})`);
    return JSON.parse(text);
  }
  function say(text, error = false) {
    const node = $('chr-feedback');
    if (node) { node.textContent = text; node.classList.toggle('error', error); }
  }
  function fileLink(id, file) {
    const link = $(id);
    link.href = `/captures/${file}`; link.download = file; link.textContent = `Download ${file}`; link.hidden = false;
  }
  const focused = () => document.visibilityState === 'visible' && document.hasFocus();
  // What an element really says, before the viewer's privacy mask (shared viewer.js); for comparisons.
  const realText = node => globalThis.RemotePilotPrivacy?.text(node) ?? node.textContent;
  // Saved files keep the real text: say so while the page hides private details.
  const fileNote = () => (globalThis.RemotePilotPrivacy?.hidden ? ' The saved file is not masked, so it can hold private details.' : '');

  function buildShell() {
    const art = deviceArt();
    const at = (node, name) => {
      const box = art.parts[name];
      node.style.cssText = `left:${box.left}%;top:${box.top}%;width:${box.width}%;height:${box.height}%`;
      return node;
    };
    const names = {UP: 'Up', DOWN: 'Down', LEFT: 'Left', RIGHT: 'Right', A: 'A', B: 'B', SELECT: 'Select', START: 'Start',
      MENU: 'Menu (right side)'};
    const buttons = BUTTONS.map(name => {
      const button = at(el('button', {type: 'button', class: `chr-btn chr-btn-${name.toLowerCase()}`, 'data-chr-button': name,
        'aria-label': names[name], 'data-tip': BUTTON_TIPS[name]}), name);
      button.innerHTML = buttonArt(name);
      return button;
    });
    const side = art.side.map(({title, left, top, width, height}, index) => {
      const spot = el('span', {class: 'chr-side', role: 'img', 'aria-label': title.split(' (')[0], tabIndex: 0,
        'data-tip': SIDE_TIPS[index], 'data-side': title.split(' (')[0]});
      spot.style.cssText = `left:${left}%;top:${top}%;width:${width}%;height:${height}%`;
      return spot;
    });
    const screen = at(el('div', {class: 'chr-screen', id: 'chr-screen',
      'data-tip': 'The console\'s screen. It shows your latest screenshot or live video from the console, at the real screen\'s 160 × 144 pixels.'},
      el('img', {id: 'chr-image', alt: 'Chromatic screen', draggable: false}),
      el('span', {class: 'chr-placeholder', id: 'chr-placeholder', text: 'No screen image'})), 'screen');
    const strip = at(el('div', {class: 'chr-bezel-top',
      'data-tip': 'Status strip: the little light turns green while this PC is connected, and the words say who is drawing the screen, the launcher menu or a game.'},
      el('span', {class: 'chr-power', id: 'chr-power', 'aria-hidden': 'true'}),
      el('span', {id: 'chr-owner', text: '160 × 144'})), 'strip');
    const shell = el('div', {id: 'chromatic-device', class: 'chr-device', 'aria-label': 'Chromatic device',
      'data-tip': 'A drawing of your Chromatic. Its buttons work: once remote controls are on, click or hold them to press the real ones.'});
    shell.innerHTML = art.svg;
    shell.append(screen, strip, ...buttons, ...side);
    const heading = el('div', {class: 'section-heading chr-stage-heading'},
      el('div', {}, el('p', {class: 'eyebrow', text: 'DEVICE VIEW · ONE SCREEN'}), el('h2', {text: 'Chromatic'})),
      el('span', {id: 'chr-sim-badge', class: 'pill',
        'data-tip': 'What this view is connected to: Console is your real Chromatic, SIMULATOR is a pretend console on this PC for practice, Off is nothing.',
        text: 'Off'}));
    const arm = el('label', {class: 'enable-line chr-arm',
      'data-tip': 'Turn this on to let this page press the console\'s buttons. It only works while this tab is open and in front, and the real buttons on the console always win.'},
      el('input', {type: 'checkbox', id: 'chr-arm'}),
      el('span', {}, el('strong', {text: 'Enable remote controls'}),
        el('small', {text: 'Buttons and controller send only while this tab is visible and focused. Physical buttons win.'})));
    const live = el('div', {class: 'chr-actions'},
      el('label', {class: 'switch-line',
        'data-tip': 'Turn on to watch the console\'s screen move in near real time. Turn it off to free the connection for other jobs.'},
        el('input', {type: 'checkbox', id: 'chr-live'}), el('span', {text: 'Live video'})),
      el('label', {class: 'switch-line',
        'data-tip': 'How many pictures per second Live video asks for (fps means frames per second). Higher looks smoother but works the connection harder.'},
        'up to ', el('select', {id: 'chr-rate', 'aria-label': 'Live video rate'},
        ...[[1000, '1 fps'], [500, '2 fps'], [200, '5 fps'], [100, '10 fps'], [66, '15 fps']]
          .map(([value, label]) => el('option', {value: String(value), text: label, selected: value === 200})))),
      el('span', {id: 'chr-fps', class: 'micro',
        'data-tip': 'How many pictures per second really arrived over the last few seconds. It can be lower than asked for while the console is busy.', text: ''}),
      el('button', {type: 'button', id: 'chr-sound', class: 'secondary', text: 'Enable sound', 'aria-pressed': 'false',
        'data-tip': 'Plays the console\'s sound through this PC\'s speakers. Press again to mute. It works only once the console can send its sound.'}));
    const captures = el('div', {class: 'chr-actions'},
      el('button', {type: 'button', id: 'chr-shot', class: 'secondary', text: 'Screenshot',
        'data-tip': 'Takes one picture of the console\'s screen and saves it on this PC. Use it to keep a record of what you see, such as a bug.'}),
      el('button', {type: 'button', id: 'chr-bundle', class: 'secondary', text: 'Capture bundle',
        'data-tip': 'Saves a screenshot, the console\'s log and a list of what was saved, handy for a bug report. The files are not masked, so check them before sharing.'}),
      el('button', {type: 'button', id: 'chr-report', class: 'secondary', text: 'Save report',
        'data-tip': 'Saves a short written report of how things stand: the connection, what works, timing and the latest messages. It is not masked, so check it before sharing.'}),
      el('label', {class: 'switch-line',
        'data-tip': 'Use a game controller plugged into this PC (an Xbox pad, for example) to press the console\'s buttons. Needs Enable remote controls on.'},
        el('input', {type: 'checkbox', id: 'chr-pad'}), el('span', {text: 'Game controller'})));
    const note = el('p', {class: 'micro', id: 'chr-control-note',
      'data-tip': 'Where button presses go right now (the launcher menu or a game), or why they cannot be sent yet.'});
    const chordNote = el('p', {class: 'micro', id: 'chr-chord',
      'data-tip': 'A chord means pressing several buttons at the same moment, like A and B together.',
      text: 'Hold a button to hold it. Hold Ctrl and click buttons, then release Ctrl, for a chord.'});
    const link = el('a', {id: 'chr-capture-link', hidden: true,
      'data-tip': 'Download the file you just saved, to open it or share it.'});
    const bubble = el('div', {class: 'chr-bubble'}, arm, live, captures, link, note, chordNote);
    const block = el('div', {id: 'chromatic-stage', class: 'chr-stage'}, heading, shell, bubble,
      el('div', {class: 'chr-perf'}, el('p', {class: 'eyebrow', text: 'TIMING'}),
        el('canvas', {id: 'chr-perf', width: 480, height: 110, 'aria-label': 'Device frame and work time',
          'data-tip': 'Timing graph: how quickly the console draws its screen. The white line is the time between pictures, the green line is how busy the console was, and the dashed line marks 60 pictures per second.'}),
        el('p', {class: 'micro', id: 'chr-perf-text',
          'data-tip': 'The newest timing numbers: pictures per second, milliseconds (thousandths of a second) per picture, and how many of those the console was busy.',
          text: 'No timing samples'})));
    document.querySelector('.stage .model-picker').after(block);
  }

  function section(title, tone, tip, ...children) {  // tone: the band colour (theme.css)
    return el('details', {class: 'chr-section', open: true, 'data-tone': tone},
      el('summary', {text: title, 'data-tip': `${tip} Click the bar to fold or unfold this card.`}), ...children);
  }

  function buildPanel() {
    const panel = el('section', {id: 'chromatic-panel', class: 'panel chr-panel', 'aria-labelledby': 'chr-title'},
      el('div', {class: 'section-heading'},
        el('div', {}, el('p', {class: 'eyebrow', text: 'CHROMATIC · USB AND LOCAL WI-FI'}), el('h2', {id: 'chr-title', text: 'Chromatic tools'})),
        el('span', {id: 'chr-phase', class: 'pill', text: 'Unknown',
          'data-tip': 'What the console is doing right now: showing the launcher menu, running a game, running its factory (stock) software, or starting up.'})),
      el('div', {id: 'chr-usb-hold', class: 'chr-hold', role: 'status', hidden: true,
        'data-tip': 'Chromatic Manager borrows the USB cable while it installs or checks the console, then gives it back. Wi-Fi keeps working meanwhile.'},
        el('strong', {id: 'chr-usb-hold-title'}), el('span', {id: 'chr-usb-hold-text'}),
        el('button', {type: 'button', id: 'chr-usb-hold-take', class: 'secondary', hidden: true, text: 'Use USB again',
          'data-tip': 'Takes the USB cable back after Chromatic Manager closed without returning it. It is refused while a Manager job is unfinished.'})),
      el('div', {class: 'chr-mode'}, el('label', {htmlFor: 'chr-mode', text: 'Connection',
        'data-tip': 'How Remote Pilot reaches the console. Choose it in the box next to this.'}),
        el('select', {id: 'chr-mode', 'aria-label': 'Chromatic connection',
          'data-tip': 'Choose how to reach the console: "Configured" is your real console over USB or Wi-Fi, the Simulated choices are practice consoles on this PC, and Off disconnects.'}),
        el('button', {type: 'button', id: 'chr-retry', class: 'secondary', text: 'Search again',
          'data-tip': 'Look for the console again right away, for example after plugging in the cable or switching the console on.'})),
      el('p', {class: 'micro',
        'data-tip': `How the link stays safe: ${CHR1}. The USB port is only opened with your permission, and the simulators never touch a real console.`,
        text: 'USB and Wi-Fi both use paired CHR1; Wi-Fi is local-network only. The USB port opens only when local/chromatic/config.json allows it, and only CHR1 lines are ever written to it. Simulators run on this PC only.'}),
      el('div', {class: 'chr-pairing', id: 'chr-usb-pair', hidden: true},
        el('strong', {id: 'chr-usb-pair-code', class: 'chr-code', 'data-private': '',
          'data-tip': 'The pairing code. Check that the console shows the same code before you press A on it.'}),
        el('span', {id: 'chr-usb-pair-text', class: 'micro', 'data-tip': 'What to do next to finish pairing.'}),
        el('button', {type: 'button', id: 'chr-usb-pair-start', text: 'Pair over USB',
          'data-tip': 'Starts pairing over the USB cable, so this PC and the console recognise each other. A code appears here and on the console; you approve it by pressing A on the console.'})),
      el('label', {class: 'switch-line', id: 'chr-pair-line', hidden: true,
        'data-tip': 'Only for the read-only USB log mode: lets Remote Pilot pick up the pairing key the launcher shows on its pairing screen. The key is stored privately on this PC and never shown.'},
        el('input', {type: 'checkbox', id: 'chr-pair'}),
        el('span', {text: 'Capture the pairing key from the launcher\'s pairing screen (stored privately, never shown)'})),
      el('dl', {class: 'chr-facts', id: 'chr-facts',
        'data-tip': 'Quick facts about the current connection. Point at a name on the left to learn what it means.'}),
      el('div', {class: 'chr-sections'},
      section('What works', 'green', 'A checklist of everything Remote Pilot can do with the console, and whether each one works right now.',
        el('div', {id: 'chr-features', class: 'chr-features'})),
      section('Logs and diagnostics', 'cyan', 'The console\'s messages, and troubleshooting files you can save on this PC.',
        el('div', {class: 'capture-actions'},
          el('button', {type: 'button', id: 'chr-log', text: 'Save log copy',
            'data-tip': 'Saves every message the console has sent so far into a text file on this PC. It is not masked, so check it before sharing.'}),
          el('button', {type: 'button', id: 'chr-clear', class: 'secondary', text: 'Clear received lines',
            'data-tip': 'Empties the message list below on this PC. The console and any saved files are not affected.'})),
        el('pre', {id: 'chr-tail', class: 'chr-tail', 'aria-label': 'Recent device lines', tabIndex: 0,
          'data-tip': 'The latest messages from the console, newest at the bottom. Useful for seeing what it is doing.'}),
        el('div', {class: 'capture-actions'},
          el('select', {id: 'chr-collect-item', 'aria-label': 'Diagnostic item',
            'data-tip': 'Choose one troubleshooting item to save: the console\'s log, a status snapshot, console answers, a summary of the Chromatic Manager\'s files, or its last job\'s output.'},
            ...[['device-log', 'Device log'], ['status', 'Status snapshot'], ['console', 'Console output'],
              ['manager-files', 'Manager files summary'], ['manager-job', 'Last Manager job output']]
              .map(([value, label]) => el('option', {value, text: label}))),
          el('button', {type: 'button', id: 'chr-collect', class: 'secondary', text: 'Collect',
            'data-tip': 'Saves the item chosen on the left as a file on this PC.'})),
        el('a', {id: 'chr-diag-link', hidden: true, 'data-tip': 'Download the file you just saved, to open it or share it.'})),
      section('Device console (read only)', 'cyan', 'Ask the console simple questions. Read only: nothing here can change the console.',
        el('div', {class: 'capture-actions'},
          el('select', {id: 'chr-console-cmd', 'aria-label': 'Console command',
            'data-tip': choiceTip('Choose a question to ask the console.', CONSOLE_TIPS, 'help')},
            ...['help', 'version', 'uptime', 'heap', 'wifi', 'phase', 'reset-reason'].map(value => el('option', {value, text: value}))),
          el('button', {type: 'button', id: 'chr-console', class: 'secondary', text: 'Run',
            'data-tip': 'Asks the console the chosen question and shows its answer below.'})),
        el('pre', {id: 'chr-console-out', class: 'chr-tail chr-short', tabIndex: 0,
          'data-tip': 'The console\'s answer to the last question you asked.'})),
      section('Firmware patching (Chromatic Manager)', 'orange',
        'Install, remove or restore the console\'s own software (its firmware) through the Chromatic Manager app. Only with the console plugged in and in front of you.',
        el('p', {class: 'micro chr-warn',
          'data-tip': 'Why this is safe: the Chromatic Manager saves a backup first, writes down every step, and can undo a failed install. Flash is the console\'s built-in storage.',
          text: 'Only with the console in front of you and plugged in. Remote Pilot never writes flash itself: it starts the Chromatic Manager, which backs up first, journals every step, protects the bootloader and settings, and undoes a failed install. There is no cancel once a write starts.'}),
        el('div', {class: 'capture-actions'},
          el('select', {id: 'chr-mgr-cmd', 'aria-label': 'Manager command',
            'data-tip': 'Choose what the Chromatic Manager should do.'}),
          el('button', {type: 'button', id: 'chr-mgr-files', class: 'secondary', text: 'Re-read Manager files',
            'data-tip': 'Reads the Chromatic Manager\'s files again to refresh the versions and backups shown here. It does not run the Manager.'})),
        el('p', {class: 'micro', id: 'chr-mgr-phrase',
          'data-tip': 'The exact words to type below to confirm. They stop a firmware change from happening by accident.'}),
        el('div', {class: 'chr-confirm'}, el('input', {id: 'chr-mgr-typed', autocomplete: 'off', placeholder: 'Type the phrase shown above',
          'data-tip': 'Type the confirmation words shown above, exactly. The Manager will not run without them.'}),
          el('button', {type: 'button', id: 'chr-mgr-run', text: 'Run Manager',
            'data-tip': 'Starts the Chromatic Manager with the chosen job. It backs up first and can undo a failed install, but there is no stopping it once writing starts.'})),
        el('p', {class: 'micro', id: 'chr-manager-state',
          'data-tip': 'What Remote Pilot found in the Chromatic Manager\'s files, such as the launcher version and how many backups exist.'}),
        el('pre', {id: 'chr-mgr-out', class: 'chr-tail chr-short', tabIndex: 0,
          'data-tip': 'Messages from the Chromatic Manager\'s current or last job, as it works.'})),
      section('Activity', 'red', 'What you have done on this page and how it went, newest first.',
        el('ol', {id: 'chr-activity', class: 'chr-activity',
          'data-tip': 'Each line shows the time, what was done and the result. Red lines are things that failed.'}))),
      el('p', {id: 'chr-feedback', class: 'feedback', role: 'status',
        'data-tip': 'The result of your last action here. Red writing means something went wrong.'}));
    document.querySelector('.workspace .stage').after(panel);
    // The smaller cards can sit under the device view; balance() decides.
    [...panel.querySelectorAll('.chr-sections > .chr-section')].slice(1).forEach(card => { card.dataset.card = 'small'; });
    $('chromatic-stage').append(el('div', {class: 'chr-more'}));
    $('connect-detail').after(el('p', {id: 'chromatic-connect-detail',
      'data-tip': 'What the console connection is doing right now, and what to do next.'}),
      el('div', {id: 'chromatic-usb-offer', class: 'chr-pairing', hidden: true},
        el('span', {id: 'chr-usb-offer-text', class: 'micro',
          'data-tip': 'Remote Pilot found your console on a USB port but has not opened it. It only opens the port after you allow it.'}),
        el('span', {id: 'chr-usb-offer-buttons'})));
  }

  // Both columns should end at about the same height. With the device view
  // and the tools side by side, the smaller cards (logs, console, firmware, activity) go, in
  // order, to whichever column is shorter. Stacked (phone width) they stay after "What works".
  // Nothing moves while one of them holds the focus, and small gains are ignored.
  function balance() {
    const stage = document.querySelector('.workspace > .stage'), panel = $('chromatic-panel');
    const left = document.querySelector('#chromatic-stage .chr-more'), right = panel?.querySelector('.chr-sections');
    if (!stage || !left || !right || document.body.dataset.preset !== ID) return;
    const cards = [...document.querySelectorAll('.chr-section[data-card="small"]')];
    if (cards.some(card => card.contains(document.activeElement))) return;
    const sideBySide = Math.abs(stage.getBoundingClientRect().top - panel.getBoundingClientRect().top) < 2 &&
      stage.getBoundingClientRect().left !== panel.getBoundingClientRect().left;
    const height = card => card.offsetHeight + 20;  // its top margin
    const inLeft = cards.filter(card => left.contains(card));
    const wanted = [];
    if (!sideBySide && !inLeft.length) return;
    if (sideBySide) {
      let l = stage.offsetHeight - inLeft.reduce((sum, card) => sum + height(card), 0);
      let r = panel.offsetHeight - cards.filter(card => !left.contains(card)).reduce((sum, card) => sum + height(card), 0);
      const now = Math.abs(stage.offsetHeight - panel.offsetHeight);
      for (const card of cards) if (l <= r) { wanted.push(card); l += height(card); } else r += height(card);
      if (wanted.length === inLeft.length && wanted.every((card, i) => card === inLeft[i])) return;
      if (Math.abs(l - r) > now - 40) return;  // not worth moving
    }
    left.append(...wanted);
    right.append(...cards.filter(card => !wanted.includes(card)));
  }
  let balanceFrame = 0;
  const scheduleBalance = () => { cancelAnimationFrame(balanceFrame); balanceFrame = requestAnimationFrame(balance); };

  // ---- buttons: hold, chords, controller -------------------------------------------
  function wantedHold() {
    return BUTTONS.filter(name => name !== 'MENU' && (pointerHeld.has(name) || padHeld.includes(name))).slice(0, 4);
  }
  async function syncHold(force = false) {
    const names = wantedHold(), key = names.join('+');
    if (!force && key === sentHold) return;
    if (!names.length && !sentHold) return;
    sentHold = key;
    try { await post('hold', {owner, buttons: names}); }
    catch (error) { say(error.message, true); }
  }
  function releaseAll() {
    pointerHeld.clear(); padHeld = [];
    document.querySelectorAll('[data-chr-button].active').forEach(button => button.classList.remove('active'));
    if (sentHold) { sentHold = ''; post('hold', {owner, buttons: []}).catch(() => {}); }
  }
  async function sendChord() {
    const names = [...chord]; chord.clear();
    document.querySelectorAll('.chord-selected').forEach(button => button.classList.remove('chord-selected'));
    if (!names.length) return;
    try { const result = await post('input', {owner, ...chordPayload(names)}); say(`Pressed ${result.buttons.join(' + ')} (${result.target}).`); }
    catch (error) { say(error.message, true); }
  }
  function wireButton(button) {
    const name = button.dataset.chrButton;
    button.addEventListener('pointerdown', event => {
      if (button.disabled) return;
      if (event.ctrlKey && name !== 'MENU') { chord.add(name); button.classList.add('chord-selected'); return; }
      if (name === 'MENU') {
        post('input', {owner, buttons: ['MENU'], ms: 100}).then(result => say(`MENU: ${result.result}.`), error => say(error.message, true));
        return;
      }
      button.setPointerCapture?.(event.pointerId);
      pointerHeld.add(name); button.classList.add('active'); syncHold();
    });
    const up = () => { if (pointerHeld.delete(name)) { button.classList.remove('active'); syncHold(); } };
    button.addEventListener('pointerup', up); button.addEventListener('pointercancel', up);
    button.addEventListener('keydown', event => {
      if ((event.key === ' ' || event.key === 'Enter') && !event.repeat && !button.disabled) {
        event.preventDefault();
        post('input', {owner, buttons: [name], ms: 100}).then(r => say(`Pressed ${name}: ${r.result}.`), e => say(e.message, true));
      }
    });
  }
  function pollPad() {
    if (!$('chr-pad').checked || !focused() || !controlState(status, 'hold').enabled) { if (padHeld.length) { padHeld = []; syncHold(); } return; }
    const pad = [...(navigator.getGamepads?.() || [])].find(Boolean);
    const names = padButtons(pad);
    if (names.join('+') !== padHeld.join('+')) { padHeld = names; syncHold(); }
  }

  // ---- live video and sound ------------------------------------------------------------
  function showImage(url, onDone) {
    const image = $('chr-image');
    image.onload = () => { $('chr-screen').classList.add('has-image'); onDone?.(true); };
    image.onerror = () => { onDone?.(false); };
    image.src = url;
  }
  function updateLive() {
    const allowed = controlState(status, 'video').enabled;
    const interval = Number($('chr-rate').value) || 200;
    if ($('chr-live').checked && allowed && (!liveTimer || liveTimer.interval !== interval)) {
      if (liveTimer) clearInterval(liveTimer.id);
      liveTimer = {interval, id: setInterval(() => {
        if (liveBusy) return;
        liveBusy = true;
        showImage(`/api/chromatic/live.bmp?n=${++frameNumber}`, ok => {
          liveBusy = false;
          if (!ok) return;
          const now = performance.now(); liveTimes = [...liveTimes.filter(t => now - t < 3000), now];
          $('chr-fps').textContent = `${(liveTimes.length / 3).toFixed(1)} fps measured`;
        });
      }, interval)};
    } else if ((!$('chr-live').checked || !allowed) && liveTimer) {
      clearInterval(liveTimer.id); liveTimer = null; $('chr-fps').textContent = '';
    }
  }
  function updateSound() {
    const on = $('chr-sound').getAttribute('aria-pressed') === 'true' && controlState(status, 'audio').enabled;
    if (on && !soundTimer) soundTimer = setInterval(fetchSound, 250);
    if (!on && soundTimer) { clearInterval(soundTimer); soundTimer = null; }
  }
  async function fetchSound() {
    try {
      const response = await fetch(`/api/chromatic/live.pcm?n=${Date.now()}`, {cache: 'no-store'});
      if (!response.ok) return;
      const data = new Int16Array(await response.arrayBuffer());
      if (!data.length || !audioContext) return;
      const buffer = audioContext.createBuffer(1, data.length, 16384);
      const channel = buffer.getChannelData(0);
      for (let i = 0; i < data.length; i++) channel[i] = data[i] / 32768;
      const source = audioContext.createBufferSource(); source.buffer = buffer; source.connect(audioContext.destination);
      audioNext = Math.max(audioNext, audioContext.currentTime + 0.05); source.start(audioNext); audioNext += buffer.duration;
    } catch (_) {}
  }

  // ---- rendering -------------------------------------------------------------------------
  function drawPerf(samples) {
    const canvas = $('chr-perf'), context = canvas.getContext('2d');
    context.clearRect(0, 0, canvas.width, canvas.height);
    if (!samples?.length) { $('chr-perf-text').textContent = 'No timing samples (needs the device timing capability).'; return; }
    const max = Math.max(33.4, ...samples.map(s => s.frameMs || 0)) * 1.1, w = canvas.width, h = canvas.height;
    const line = (key, color) => {
      context.strokeStyle = color; context.lineWidth = 2; context.beginPath();
      samples.forEach((sample, index) => {
        const x = samples.length < 2 ? w : index * w / (samples.length - 1), y = h - (sample[key] || 0) / max * h;
        index ? context.lineTo(x, y) : context.moveTo(x, y);
      });
      context.stroke();
    };
    context.strokeStyle = '#666666'; context.setLineDash([4, 4]); context.beginPath();
    context.moveTo(0, h - 16.7 / max * h); context.lineTo(w, h - 16.7 / max * h); context.stroke(); context.setLineDash([]);
    line('frameMs', '#ffffff'); line('workMs', '#80ff00');  // Chromatic Manager text and green
    const last = samples[samples.length - 1];
    $('chr-perf-text').textContent = `${last.fps} fps · frame ${last.frameMs} ms · work ${last.workMs} ms · 16.7 ms guide`;
  }

  function renderFeatures() {
    const groups = new Map();
    for (const item of status.features || []) {
      if (!groups.has(item.group)) groups.set(item.group, []);
      groups.get(item.group).push(item);
    }
    $('chr-features').replaceChildren(...[...groups].map(([group, items]) => el('div', {class: 'chr-group'},
      el('h4', {text: GROUPS[group] || group, 'data-tip': GROUP_TIPS[group] || 'A group of related features.'}),
      el('ul', {}, ...items.map(item => {
        const {text, tone} = badge(item.state);
        return el('li', {'data-state': item.state, 'data-feature': item.id,
          'data-tip': FEATURE_TIPS[item.id] || 'One thing Remote Pilot can do with the console. The badge says whether it works now.'},
          el('span', {class: `chr-badge chr-${tone}`, text,
            'data-tip': STATE_TIPS[item.state] || 'Unknown: Remote Pilot has no status for this yet.'}),
          el('strong', {text: item.label}), el('small', {text: item.detail}));
      })))));
  }

  function renderManager() {
    const manager = status.manager, job = status.managerJob;
    const select = $('chr-mgr-cmd');
    if (select.options.length !== (status.managerCommands || []).length) {
      select.replaceChildren(...(status.managerCommands || []).map(value => el('option', {value, text: value})));
      select.dispatchEvent(new Event('change'));  // show the phrase for the first command
    }
    select.dataset.tip = choiceTip('Choose what the Chromatic Manager should do.', MANAGER_TIPS, select.value);
    const parts = [];
    if (manager?.projectFound) parts.push(`Launcher source ${manager.launcherSourceVersion || 'unknown'}; backup folders ${manager.backupFolders}.`);
    if (manager) parts.push(manager.detail);
    $('chr-manager-state').textContent = parts.join(' ');
    const patch = feature(status, 'firmware-patch');
    const allowed = patch.state === 'available' && status.localClient && !(job && job.state === 'running');
    $('chr-mgr-run').disabled = !allowed;
    $('chr-mgr-run').title = !status.localClient ? 'Only from this PC\'s own browser' : patch.state !== 'available' ? patch.detail :
      job?.state === 'running' ? 'A Manager job is running' : '';
    if (job) {
      const text = [`${job.command}: ${job.state}${job.exitCode == null ? '' : ` (exit ${job.exitCode})`}`, ...job.lines].join('\n');
      if (realText($('chr-mgr-out')) !== text) $('chr-mgr-out').textContent = text;
    }
  }

  function render() {
    if (!status) return;
    const t = status.transport || {};
    const select = $('chr-mode');
    const options = modeOptions(status);
    if (select.options.length !== options.length || [...select.options].some((o, i) => o.textContent !== options[i].label))
      select.replaceChildren(...options.map(option => el('option', {value: option.value, text: option.label})));
    const current = SIM_MODES.includes(status.mode) ? status.mode : 'configured';
    if (document.activeElement !== select) select.value = current;
    $('chr-retry').hidden = !['lan', 'usb', 'auto'].includes(status.mode);
    const pairing = pairingView(status);
    $('chr-usb-pair').hidden = !pairing.visible;
    $('chr-usb-pair-code').textContent = pairing.code || '';
    $('chr-usb-pair-text').textContent = pairing.text;
    $('chr-usb-pair-start').disabled = !pairing.canStart;
    $('chr-pair-line').hidden = t.kind !== 'serial';
    if (document.activeElement !== $('chr-pair')) $('chr-pair').checked = Boolean(status.pairingArmed);
    $('chr-sim-badge').textContent = t.simulated ? 'SIMULATOR' : status.mode === 'off' ? 'Off' : 'Console';
    $('chr-sim-badge').classList.toggle('chr-simulated', Boolean(t.simulated));
    $('chr-phase').textContent = {launcher: 'Launcher running', game: 'Game running', stock: 'Stock firmware', booting: 'Booting'}[t.phase] || 'Unknown';
    $('chr-owner').textContent = t.target === 'game' ? 'GAME · 160 × 144' : 'LAUNCHER · 160 × 144';
    $('chr-power').classList.toggle('on', Boolean(t.connected));
    const facts = [['Connection', t.label || 'Off'], ['Launcher', t.launcherVersion || '—'], ['Phase', t.phase || 'unknown'],
      ['Screen owner', t.target || '—'], ['Held', t.held || '—'], ['Last reset', t.resetReason || '—'],
      ['Lines received', String(t.lines ?? 0)], ['Last contact', t.lastLineAgeS == null ? '—' : `${t.lastLineAgeS} s ago`]];
    if (t.route || t.kind === 'usb' || t.kind === 'lan') facts.push(['Link in use', t.session ? (t.kind === 'usb' ? 'USB' : 'Wi-Fi') : 'none yet']);
    if (t.usbState) facts.push(['USB', t.usbState]);
    if (t.leaseLeftMs != null) facts.push(['Session lease', `${Math.round(t.leaseLeftMs / 1000)} s left`]);
    if (t.keyKind) facts.push(['Key', t.keyKind]);
    if (t.host) facts.push(['Address', t.host]);
    if (t.error || status.configError) facts.push(['Note', t.error || status.configError]);
    $('chr-facts').replaceChildren(...facts.flatMap(([key, value]) => {
      const tip = FACT_TIPS[key] || 'A fact about the current connection.';
      return [el('dt', {text: key, 'data-tip': tip}), el('dd', {text: value, 'data-tip': tip})];
    }));
    renderFeatures();
    const tail = $('chr-tail'), text = (status.tail || []).join('\n') || 'No lines yet.';
    if (realText(tail) !== text) { tail.textContent = text; tail.scrollTop = tail.scrollHeight; }
    const hold = controlState(status, 'hold'), input = controlState(status, 'input'), shot = controlState(status, 'shot');
    const video = controlState(status, 'video'), log = controlState(status, 'log'), audio = controlState(status, 'audio');
    const consoleState = controlState(status, 'console');
    document.querySelectorAll('[data-chr-button]').forEach(button => {
      const menu = button.dataset.chrButton === 'MENU';
      const state = menu ? input : hold.enabled ? hold : input;
      button.disabled = !state.enabled;
      button.title = state.enabled ? '' : state.reason;  // the tooltip explains the button; this adds why it is off
    });
    if (!hold.enabled && (pointerHeld.size || padHeld.length)) releaseAll();
    $('chr-shot').disabled = !shot.enabled; $('chr-shot').title = shot.reason;
    $('chr-live').disabled = !video.enabled;
    $('chr-sound').disabled = !audio.enabled; $('chr-sound').title = audio.reason;
    $('chr-bundle').disabled = status.mode === 'off' || !status.linkEnabled;
    $('chr-log').disabled = !log.enabled; $('chr-log').title = log.reason;
    $('chr-clear').disabled = status.mode === 'off';
    $('chr-console').disabled = !consoleState.enabled; $('chr-console').title = consoleState.reason;
    $('chr-pad').disabled = !hold.enabled && !$('chr-pad').checked;
    $('chr-control-note').textContent = input.enabled ?
      (t.target === 'game' ? 'Buttons go to the running game through the device\'s FPGA game bridge.' :
        'Buttons go to the launcher menu. Games need an FPGA update before buttons can reach them.') :
      `Device buttons: ${input.reason}`;
    $('chr-placeholder').textContent = video.enabled || shot.enabled ? 'Take a screenshot or turn on Live video' :
      `Screen: ${shot.reason}`;
    if (!shot.enabled && !video.enabled) $('chr-screen').classList.remove('has-image');
    drawPerf(status.perf);
    renderManager();
    $('chr-activity').replaceChildren(...(status.activity || []).slice().reverse().map(item =>
      el('li', {class: item.ok ? '' : 'error', text: `${item.time} ${item.action} · ${item.detail}`})));
    const offer = usbOfferView(status);
    $('chromatic-usb-offer').hidden = !offer.visible;
    $('chr-usb-offer-text').textContent = offer.text;
    const buttons = $('chr-usb-offer-buttons');
    if (buttons.dataset.ports !== offer.ports.join(',')) {
      buttons.dataset.ports = offer.ports.join(',');
      buttons.replaceChildren(...offer.ports.map(port => {
        const button = el('button', {type: 'button', text: `Allow USB on ${port}`,
          'data-tip': `Lets Remote Pilot open ${port}, the USB port your console is plugged into, so it can talk to the console without Wi-Fi. Your choice is saved only on this PC.`});
        button.addEventListener('click', () => allowUsb(port, button));
        return button;
      }));
    }
    buttons.querySelectorAll('button').forEach(button => { button.disabled = !offer.canAllow; });
    const detail = connectText(status);
    if (realText($('chromatic-connect-detail')) !== detail) $('chromatic-connect-detail').textContent = detail;
    renderHold();
    updateLive(); updateSound();
    scheduleBalance();
  }

  function renderHold() {
    const paused = Boolean(status?.usbHold);
    if (holdShown && !paused) holdBackUntil = Date.now() + 8000;
    holdShown = paused;
    const view = usbHoldView(status, !paused && Date.now() < holdBackUntil);
    const box = $('chr-usb-hold');
    box.hidden = !view.visible;
    box.dataset.state = view.state;
    $('chr-usb-hold-title').textContent = view.title;
    $('chr-usb-hold-text').textContent = view.text;
    $('chr-usb-hold-take').hidden = !view.canTake;
  }

  async function refresh() {
    try {
      const response = await fetch('/api/chromatic/status', {cache: 'no-store'});
      const text = await response.text();
      if (!response.ok) throw new Error(text || 'Chromatic status unavailable');
      status = JSON.parse(text);
      render();
    } catch (error) { say(error.message, true); }
  }

  async function allowUsb(port, button) {
    button.disabled = true;
    try { const result = await post('allow-usb', {port}); say(`${result.result}: connecting (USB first, then Wi-Fi).`); }
    catch (error) { say(error.message, true); }
    refresh();
  }

  async function arm() {
    const want = $('chr-arm').checked && focused();
    if (!want && !status?.controlsArmed) return;
    try { await post('arm', {owner, enabled: want}); }
    catch (error) { $('chr-arm').checked = false; say(error.message, true); }
    if (!want) releaseAll();
  }

  function build() {
    if (built) return;
    built = true;
    buildShell();
    buildPanel();
    document.querySelectorAll('[data-chr-button]').forEach(wireButton);
    window.addEventListener('resize', scheduleBalance);
    scheduleBalance();
    document.addEventListener('keyup', event => { if (event.key === 'Control' && chord.size) sendChord(); });
    window.addEventListener('blur', () => { releaseAll(); arm(); });
    document.addEventListener('visibilitychange', () => { if (document.visibilityState !== 'visible') { releaseAll(); arm(); } });
    $('chr-arm').addEventListener('change', () => { arm().then(refresh); });
    $('chr-mode').addEventListener('change', async event => {
      try { await post('mode', {mode: event.target.value}); say(`Connection: ${event.target.selectedOptions[0].textContent}.`); }
      catch (error) { say(error.message, true); }
      refresh();
    });
    $('chr-retry').addEventListener('click', () => post('retry').then(r => say(`Searching: ${r.scan}.`), e => say(e.message, true)));
    $('chr-usb-pair-start').addEventListener('click', () => post('pair-usb', {start: true})
      .then(() => { say('Asked the console to pair. Press A on the console if both codes match.'); refresh(); }, e => say(e.message, true)));
    $('chr-pair').addEventListener('change', event => post('pair-capture', {enabled: event.target.checked})
      .then(() => say(event.target.checked ? 'Waiting for the launcher\'s pairing line on USB serial.' : 'Pairing capture off.'), e => say(e.message, true)));
    $('chr-shot').addEventListener('click', async () => {
      try { const result = await post('shot'); showImage(`/captures/${result.file}`); fileLink('chr-capture-link', result.file); say(`Saved ${result.file} (${result.target}).${fileNote()}`); }
      catch (error) { say(error.message, true); }
    });
    $('chr-bundle').addEventListener('click', async () => {
      try {
        const result = await post('bundle'); fileLink('chr-capture-link', result.file);
        const skipped = Object.entries(result.skipped || {}).map(([k, v]) => `${k}: ${v}`).join('; ');
        say(`Saved bundle ${result.file}${skipped ? ` (skipped ${skipped})` : ''}.${fileNote()}`);
      } catch (error) { say(error.message, true); }
    });
    $('chr-report').addEventListener('click', async () => {
      try { const result = await post('report'); fileLink('chr-capture-link', result.file); say(`Saved ${result.file}.${fileNote()}`); }
      catch (error) { say(error.message, true); }
    });
    $('chr-log').addEventListener('click', async () => {
      try { const result = await post('log'); fileLink('chr-diag-link', result.file); say(`Saved ${result.bytes} bytes to local/captures/${result.file}.${fileNote()}`); }
      catch (error) { say(error.message, true); }
    });
    $('chr-clear').addEventListener('click', () => post('clear-log').then(() => { say('Cleared the received lines on this PC.'); refresh(); }, e => say(e.message, true)));
    $('chr-collect').addEventListener('click', async () => {
      try { const result = await post('collect', {item: $('chr-collect-item').value}); fileLink('chr-diag-link', result.file); say(`Saved ${result.file}.${fileNote()}`); }
      catch (error) { say(error.message, true); }
    });
    $('chr-console').addEventListener('click', async () => {
      try { const result = await post('console', {command: $('chr-console-cmd').value}); $('chr-console-out').textContent = `> ${result.command}\n${result.result}`; }
      catch (error) { say(error.message, true); }
    });
    const showPhrase = async () => {
      try { const result = await post('manager-phrase', {command: $('chr-mgr-cmd').value}); $('chr-mgr-phrase').textContent = `To run "${result.command}", type: ${result.phrase}`; }
      catch (error) { $('chr-mgr-phrase').textContent = error.message; }
    };
    $('chr-mgr-cmd').addEventListener('change', showPhrase);
    $('chr-console-cmd').addEventListener('change', event => {
      event.target.dataset.tip = choiceTip('Choose a question to ask the console.', CONSOLE_TIPS, event.target.value);
    });
    $('chr-mgr-files').addEventListener('click', () => post('manager').then(() => { say('Re-read the Manager files. The Manager was not run.'); showPhrase(); refresh(); }, e => say(e.message, true)));
    $('chr-mgr-run').addEventListener('click', async () => {
      try {
        await post('manager-run', {command: $('chr-mgr-cmd').value, phrase: $('chr-mgr-typed').value});
        $('chr-mgr-typed').value = ''; say('The Chromatic Manager is running. Keep the console plugged in.');
      } catch (error) { say(error.message, true); }
      refresh();
    });
    $('chr-usb-hold-take').addEventListener('click', async () => {
      try { await post('usb-return', {force: true}); say('USB is back with Remote Pilot.'); }
      catch (error) { say(error.message, true); }
      refresh();
    });
    $('chr-live').addEventListener('change', updateLive);
    $('chr-rate').addEventListener('change', updateLive);
    $('chr-sound').addEventListener('click', () => {
      const on = $('chr-sound').getAttribute('aria-pressed') !== 'true';
      $('chr-sound').setAttribute('aria-pressed', String(on)); $('chr-sound').textContent = on ? 'Mute sound' : 'Enable sound';
      if (on && !audioContext) { audioContext = new AudioContext(); audioNext = 0; }
      updateSound();
    });
  }

  function presetChanged() {
    const active = document.body.dataset.preset === ID;
    if (active) {
      build();
      if (!timer) {
        refresh(); timer = setInterval(refresh, 1000);
        armTimer = setInterval(arm, 500);
        holdTimer = setInterval(() => { if (sentHold) syncHold(true); }, 200);  // refresh the device's 600 ms lease
        padTimer = setInterval(pollPad, 50);
      }
    } else {
      for (const id of [timer, armTimer, holdTimer, padTimer, soundTimer]) if (id) clearInterval(id);
      if (liveTimer) clearInterval(liveTimer.id);
      timer = armTimer = holdTimer = padTimer = soundTimer = liveTimer = null;
      if (built) {
        releaseAll(); $('chr-live').checked = false; $('chr-pad').checked = false;
        if ($('chr-arm').checked) { $('chr-arm').checked = false; post('arm', {owner, enabled: false}).catch(() => {}); }
      }
    }
  }

  new MutationObserver(presetChanged).observe(document.body, {attributes: true, attributeFilter: ['data-preset']});
  presetChanged();
})();
