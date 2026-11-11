# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Chromatic PC-tool theme: the look of ModRetro's official Chromatic Firmware Updater, so
this project's own PC tools read as siblings of it ("exactly,
as if they were the same family of tools").

What is matched, from the owner's three screenshots of the updater (System Update, Cart
Clinic, About) and the colour values recorded earlier from its style sheets and images
(OFFICIAL_UPDATER below): a compact black window with a thick border in each tab's colour,
rounded pixel tabs with icons at the top left and a red close box at the top right, a
full-width header band in the tab's colour with a big black title and a dithered fade at
its right end, a console drawing on the left, a black speech-bubble panel with a grey
bevelled border pointing at it, bevelled pixel buttons (grey, green, red), yellow notice
text, faint line doodles in the background and a small multicolour name at the bottom right.

What is NOT copied: ModRetro's images, logo, wordmarks ("MODRETRO", "CHROMATIC"), mascot,
fonts and doodle artwork. Everything here is drawn in code: our own pixel console (shaped
like the Chromatic it manages, with our own face and status screens), our own tab icons,
our own doodles and our own multicolour "Chromatic Manager" pixel logo. The updater's
licence (EULA 2.3 to 2.5) forbids redistributing or modifying it.

Font: drop open-licensed pixel fonts (.ttf/.otf, with their OFL.txt) into
scripts/chromatic_gui_fonts/. A file whose name contains "head" or "title" is the heading
font, one with "body" the body font; otherwise the first file is used for both. Without
any, headings are drawn small and scaled up pixel by pixel, and body text uses a crisp
monospace font without smoothing.

Other project tools reuse this by `import chromatic_theme` then `chromatic_theme.apply(app)`.
"""
from __future__ import annotations

from pathlib import Path
import sys

QSS = Path(__file__).with_name('chromatic_theme.qss')
FONT_FOLDER = Path(__file__).with_name('chromatic_gui_fonts')

UPD = "ModRetro's updater (recorded colour value or the owner's screenshots)"
OWN = "own choice (not ModRetro's)"

# name: (value, source)
PALETTE_SOURCES = {
    'window':          ('#000000', UPD + ': black body'),
    'doodle':          ('#2c2c2c', UPD + ': faint background line colour (our own doodles)'),
    'frame_dim':       ('#666666', UPD + ': panel bevel'),
    'frame_light':     ('#9a9a9a', UPD + ': panel bevel, lit edge'),
    'frame_shadow':    ('#333333', UPD + ': panel bevel shadow'),
    'tab_idle':        ('#7a7a7a', UPD + ': inactive tab outline and icon'),
    'tab_idle_fill':   ('#1e1e1e', UPD + ': inactive tab face'),
    'text':            ('#ffffff', UPD + ': text'),
    'text_dim':        ('#999999', UPD + ': secondary text'),
    'text_faint':      ('#5c5c5c', OWN + ': disabled text'),
    # Tab colours: border, band and active tab
    'tab_home':        ('#ff0000', UPD + ': SYSTEM UPDATE red'),
    'tab_fix':         ('#ff9900', UPD + ': CART CLINIC orange'),
    'tab_settings':    ('#00a2ff', OWN + ': a fourth tab colour, the cyan of the updater\'s screen art'),
    'tab_about':       ('#66cb01', UPD + ': ABOUT green'),
    'tab_pilot':       ('#b44dff', OWN + ': a fifth tab colour for Remote Pilot, a violet between the cyan and '
                                     'the purple of the logo'),
    # Meanings
    'ok':              ('#32ff00', UPD + ': success / up to date green'),
    'warn':            ('#ffcc00', UPD + ': yellow notice text / update available'),
    'error':           ('#ff3b2f', UPD + ': error red'),
    'info':            ('#00a2ff', OWN + ': plain information'),
    'link':            ('#12f412', UPD + ': link green'),
    # Pixel buttons: face, lit edge, shade, outline
    'button':          ('#cccccc', UPD + ': grey button face'),
    'button_hi':       ('#eeeeee', UPD + ': grey button lit edge'),
    'button_shade':    ('#8c8c8c', UPD + ': grey button shade'),
    'button_go':       ('#80ff00', UPD + ': green button face'),
    'button_go_hi':    ('#c8ff8a', OWN + ': green button lit edge'),
    'button_go_shade': ('#4c9700', UPD + ': green button shade'),
    'button_stop':     ('#ff1a1a', UPD + ': red button face'),
    'button_stop_hi':  ('#ff8a8a', OWN + ': red button lit edge'),
    'button_stop_shade': ('#a00000', UPD + ': red button shade'),
    'button_warn':     ('#ffcc00', OWN + ': amber button face (update available)'),
    'button_warn_hi':  ('#ffe98a', OWN + ': amber button lit edge'),
    'button_warn_shade': ('#b88a00', OWN + ': amber button shade'),
    'outline':         ('#111111', OWN + ': button outline'),
    # Progress (13 green segments, like the updater's)
    'progress_hi':     ('#80ff00', UPD + ': lit segment, top'),
    'progress_mid':    ('#66cb01', UPD + ': lit segment'),
    'progress_lo':     ('#4c9700', UPD + ': lit segment, bottom'),
    'progress_off_hi': ('#6f6f6f', UPD + ': unlit segment, top'),
    'progress_off_mid': ('#424242', UPD + ': unlit segment'),
    'progress_off_lo': ('#232323', UPD + ': unlit segment, bottom'),
}
PALETTE = {name: value for name, (value, _) in PALETTE_SOURCES.items()}

# Values recorded earlier, read-only, from the updater 1.7.5 (S = its style sheets, D = its
# Designer screens, R = pixels of its images, measured locally and never shipped).
OFFICIAL_UPDATER = {
    'window':          ('#000000', 'R bg.png: 206,760 of 270,000 pixels are #000000'),
    'text':            ('#ffffff', 'F FlasherForm stylesheet "color: rgb(255, 255, 255)"'),
    'text_dim':        ('#999999', 'D system_tab/check/connect/updating "color: rgb(153,153,153)"'),
    'accent_system':   ('#ff0000', 'R static_tab1.png: SYSTEM UPDATE border and title band'),
    'accent_cart':     ('#ff9900', 'R static_tab2.png: CART CLINIC border and title band'),
    'accent_about':    ('#66cb01', 'R static_tab3.png: ABOUT border and title band'),
    'ok':              ('#32ff00', 'D system_success/update/uptodate "color: rgb(50, 255, 0)"'),
    'warn':            ('#ffcc00', 'D system_update "color: rgb(255,204,0)"'),
    'error':           ('#ff0000', 'D system_error "color: rgb(255, 0, 0)"'),
    'link':            ('#12f412', 'D about_screen / system_error links "color:#12f412"'),
    'yes':             ('#777777', 'S yes_button.qss / input_dialog.qss background'),
    'yes_hover':       ('#6a6a6a', 'S yes_button.qss :hover'),
    'yes_pressed':     ('#5c5c5c', 'S yes_button.qss :pressed'),
    'no':              ('#454545', 'S no_button.qss background'),
    'no_hover':        ('#3e3e3e', 'S no_button.qss :hover'),
    'no_pressed':      ('#353535', 'S no_button.qss :pressed'),
    'button':          ('#cccccc', 'R btn_blank.png face'),
    'button_go':       ('#80ff00', 'R btn_green_medium.png face'),
    'button_stop':     ('#ff0000', 'R btn_red_medium.png face'),
    'progress_hi':     ('#80ff00', 'R progress_green.png rows 0-1'),
}

TAB_COLOURS = {'console': 'tab_home', 'pilot': 'tab_pilot', 'recovery': 'tab_fix', 'settings': 'tab_settings',
               'about': 'tab_about'}

GEOMETRY = {
    'window': (680, 470),           # fixed, like the updater's compact 625x432 window
    'window_log': (680, 620),       # with the log panel open
    'base_point_size': 9,
    'small_point_size': 8,
    'title_scale': 3,               # header band title: 8-pixel letters drawn 3x
    'progress_segment': (17, 34),   # the updater's 13 segments, 17 wide, 20 apart
    'progress_segments': 13,
    'progress_step': 20,
}

SCREEN_FLOW = ('connect', 'check', 'update', 'updating', 'success', 'error')

MONO_FAMILIES = ('Consolas', 'Cascadia Mono', 'Lucida Console', 'Courier New')
FONTS = {'heading': None, 'body': None}  # families loaded from FONT_FOLDER by load_fonts()

TONES = {'ok': 'ok', 'update': 'warn', 'error': 'error', 'info': 'info', 'busy': 'warn'}


def font_folders():
    folders = [FONT_FOLDER]
    bundled = Path(getattr(sys, '_MEIPASS', '')) / 'chromatic_gui_fonts'
    if getattr(sys, '_MEIPASS', None) and bundled != FONT_FOLDER:
        folders.append(bundled)
    return folders


def load_fonts():
    """Load pixel fonts from chromatic_gui_fonts/ (if any). Returns FONTS."""
    from PySide6.QtGui import QFontDatabase
    found = []
    for folder in font_folders():
        if folder.is_dir():
            found += sorted(p for p in folder.iterdir() if p.suffix.lower() in ('.ttf', '.otf'))
        if found:
            break
    families = {}
    for path in found:
        ident = QFontDatabase.addApplicationFont(str(path))
        names = QFontDatabase.applicationFontFamilies(ident) if ident >= 0 else []
        if names:
            families[path.stem.lower()] = names[0]
    pick = lambda *keys: next((f for stem, f in families.items() if any(k in stem for k in keys)), None)
    first = next(iter(families.values()), None)
    FONTS['heading'] = pick('head', 'title') or first
    FONTS['body'] = pick('body', 'text') or first
    return FONTS


def body_font(size=None, bold=False):
    """The body font: the pixel font if loaded, else a crisp monospace without smoothing."""
    from PySide6.QtGui import QFont
    font = QFont()
    if FONTS['body']:
        font.setFamily(FONTS['body'])
    else:
        font.setFamilies(list(MONO_FAMILIES))
        font.setStyleHint(QFont.StyleHint.Monospace)
    font.setStyleStrategy(QFont.StyleStrategy.NoAntialias)
    font.setPointSizeF(size or GEOMETRY['base_point_size'])
    font.setBold(bold)
    return font


def heading_font(size=None):
    from PySide6.QtGui import QFont
    font = body_font(size or GEOMETRY['base_point_size'] + 2, True)
    if FONTS['heading']:
        font.setFamily(FONTS['heading'])
    return font


def stylesheet(accent=None):
    """The application style sheet with palette names filled in (`accent`: a palette name
    or None for the Home tab's colour)."""
    text = QSS.read_text(encoding='utf-8')
    values = dict(PALETTE, accent=PALETTE.get(accent or 'tab_home', PALETTE['tab_home']))
    for name in sorted(values, key=len, reverse=True):
        text = text.replace('@' + name, values[name])
    return text


# ---- pixel artwork, all our own, drawn in code -------------------------------------------------
# Our own 5x7 pixel letters (rows top to bottom, '#' lit; an 8th row is a descender), used
# for headings and the logo until an open-licensed pixel font is added. Drawn for this project.
PIXEL_GLYPHS = {
    'A': '.###.|#...#|#...#|#####|#...#|#...#|#...#', 'B': '####.|#...#|#...#|####.|#...#|#...#|####.',
    'C': '.###.|#...#|#....|#....|#....|#...#|.###.', 'D': '###..|#..#.|#...#|#...#|#...#|#..#.|###..',
    'E': '#####|#....|#....|####.|#....|#....|#####', 'F': '#####|#....|#....|####.|#....|#....|#....',
    'G': '.###.|#...#|#....|#.###|#...#|#...#|.####', 'H': '#...#|#...#|#...#|#####|#...#|#...#|#...#',
    'I': '.###.|..#..|..#..|..#..|..#..|..#..|.###.', 'J': '..###|...#.|...#.|...#.|...#.|#..#.|.##..',
    'K': '#...#|#..#.|#.#..|##...|#.#..|#..#.|#...#', 'L': '#....|#....|#....|#....|#....|#....|#####',
    'M': '#...#|##.##|#.#.#|#.#.#|#...#|#...#|#...#', 'N': '#...#|#...#|##..#|#.#.#|#..##|#...#|#...#',
    'O': '.###.|#...#|#...#|#...#|#...#|#...#|.###.', 'P': '####.|#...#|#...#|####.|#....|#....|#....',
    'Q': '.###.|#...#|#...#|#...#|#.#.#|#..#.|.##.#', 'R': '####.|#...#|#...#|####.|#.#..|#..#.|#...#',
    'S': '.####|#....|#....|.###.|....#|....#|####.', 'T': '#####|..#..|..#..|..#..|..#..|..#..|..#..',
    'U': '#...#|#...#|#...#|#...#|#...#|#...#|.###.', 'V': '#...#|#...#|#...#|#...#|#...#|.#.#.|..#..',
    'W': '#...#|#...#|#...#|#.#.#|#.#.#|#.#.#|.#.#.', 'X': '#...#|#...#|.#.#.|..#..|.#.#.|#...#|#...#',
    'Y': '#...#|#...#|.#.#.|..#..|..#..|..#..|..#..', 'Z': '#####|....#|...#.|..#..|.#...|#....|#####',
    '0': '.###.|#...#|#..##|#.#.#|##..#|#...#|.###.', '1': '..#..|.##..|..#..|..#..|..#..|..#..|.###.',
    '2': '.###.|#...#|....#|...#.|..#..|.#...|#####', '3': '#####|...#.|..#..|...#.|....#|#...#|.###.',
    '4': '...#.|..##.|.#.#.|#..#.|#####|...#.|...#.', '5': '#####|#....|####.|....#|....#|#...#|.###.',
    '6': '..##.|.#...|#....|####.|#...#|#...#|.###.', '7': '#####|....#|...#.|..#..|.#...|.#...|.#...',
    '8': '.###.|#...#|#...#|.###.|#...#|#...#|.###.', '9': '.###.|#...#|#...#|.####|....#|...#.|.##..',
    'a': '.....|.....|.###.|....#|.####|#...#|.####', 'b': '#....|#....|####.|#...#|#...#|#...#|####.',
    'c': '.....|.....|.###.|#....|#....|#...#|.###.', 'd': '....#|....#|.####|#...#|#...#|#...#|.####',
    'e': '.....|.....|.###.|#...#|#####|#....|.###.', 'f': '..##.|.#..#|.#...|###..|.#...|.#...|.#...',
    'g': '.....|.....|.####|#...#|#...#|.####|....#|.###.', 'h': '#....|#....|#.##.|##..#|#...#|#...#|#...#',
    'i': '..#..|.....|.##..|..#..|..#..|..#..|.###.', 'j': '...#.|.....|..##.|...#.|...#.|#..#.|.##..',
    'k': '#....|#....|#..#.|#.#..|##...|#.#..|#..#.', 'l': '.##..|..#..|..#..|..#..|..#..|..#..|.###.',
    'm': '.....|.....|##.#.|#.#.#|#.#.#|#...#|#...#', 'n': '.....|.....|#.##.|##..#|#...#|#...#|#...#',
    'o': '.....|.....|.###.|#...#|#...#|#...#|.###.', 'p': '.....|.....|####.|#...#|#...#|####.|#....|#....',
    'q': '.....|.....|.####|#...#|#...#|.####|....#|....#', 'r': '.....|.....|#.##.|##..#|#....|#....|#....',
    's': '.....|.....|.####|#....|.###.|....#|####.', 't': '.#...|.#...|###..|.#...|.#...|.#..#|..##.',
    'u': '.....|.....|#...#|#...#|#...#|#..##|.##.#', 'v': '.....|.....|#...#|#...#|#...#|.#.#.|..#..',
    'w': '.....|.....|#...#|#...#|#.#.#|#.#.#|.#.#.', 'x': '.....|.....|#...#|.#.#.|..#..|.#.#.|#...#',
    'y': '.....|.....|#...#|#...#|#...#|.####|....#|.###.', 'z': '.....|.....|#####|...#.|..#..|.#...|#####',
    '-': '.....|.....|.....|#####|.....|.....|.....', '.': '.....|.....|.....|.....|.....|.....|..#..',
    ':': '.....|..#..|.....|.....|.....|..#..|.....', '!': '..#..|..#..|..#..|..#..|..#..|.....|..#..',
    '?': '.###.|#...#|....#|...#.|..#..|.....|..#..', "'": '..#..|..#..|.....|.....|.....|.....|.....',
    '/': '....#|....#|...#.|..#..|.#...|#....|#....', ',': '.....|.....|.....|.....|.....|..#..|.#...',
    '(': '...#.|..#..|.#...|.#...|.#...|..#..|...#.', ')': '.#...|..#..|...#.|...#.|...#.|..#..|.#...',
    '+': '.....|..#..|..#..|#####|..#..|..#..|.....', '%': '##..#|##.#.|...#.|..#..|.#...|.#.##|#..##',
}


def pixel_text_image(text, colours, scale=3, base_px=8, bold=True):
    """Text as chunky pixels, scaled up `scale` times nearest-neighbour. With a heading pixel
    font loaded (chromatic_gui_fonts/) it is drawn in that font at `base_px` pixels; without
    one, in our own 5x7 letters above (made heavier with `bold`, like the updater's titles).
    `colours`: one colour, or a list cycled per letter. A transparent QImage."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QFontMetrics, QImage, QPainter
    colours = [colours] if isinstance(colours, str) else list(colours)
    if FONTS['heading']:
        small = heading_font()
        small.setPixelSize(base_px)
        metrics = QFontMetrics(small)
        # Letters are drawn one at a time, so the image is as wide as their own advances added up
        # (the whole string's advance can be a few pixels less, which cut off the last letter).
        width = max(metrics.horizontalAdvance(text), sum(metrics.horizontalAdvance(ch) for ch in text))
        image = QImage(max(1, width + 2), metrics.height(), QImage.Format.Format_ARGB32)
        image.fill(Qt.GlobalColor.transparent)
        p = QPainter(image)
        p.setFont(small)
        x, n = 1, 0
        for ch in text:
            p.setPen(QColor(colours[n % len(colours)]))
            p.drawText(x, metrics.ascent(), ch)
            x += metrics.horizontalAdvance(ch)
            n += 1 if ch.strip() else 0
        p.end()
    else:
        advance = 7 if bold else 6
        widths = [3 if ch == ' ' else advance for ch in text]
        image = QImage(max(1, sum(widths) + 1), 9, QImage.Format.Format_ARGB32)
        image.fill(Qt.GlobalColor.transparent)
        x, n = 0, 0
        for ch, width in zip(text, widths):
            glyph = PIXEL_GLYPHS.get(ch) or PIXEL_GLYPHS.get(ch.upper()) or PIXEL_GLYPHS['?']
            if ch != ' ':
                ink = QColor(colours[n % len(colours)]).rgba()
                for row, line in enumerate(glyph.split('|')):
                    for col, cell in enumerate(line):
                        if cell == '#':
                            image.setPixel(x + col, row, ink)
                            # heavier strokes, but never filling a one-pixel gap (M, W, N stay readable)
                            if bold and not (col + 2 < len(line) and line[col + 2] == '#' and line[col + 1] == '.'):
                                image.setPixel(x + col + 1, row, ink)
                n += 1
            x += width
    if scale > 1:
        image = image.scaled(image.width() * scale, image.height() * scale,
                             Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.FastTransformation)
    return image


# Tab icons on a 9x9 grid ('#' = lit).
TAB_ICONS = {
    'console': ('....#....', '...###...', '..#####..', '.#######.', '#########', '.#.....#.', '.#.###.#.',
                '.#.#.#.#.', '.#.#.#.#.'),                       # a house: Home
    'recovery': ('.##...##.', '.#.....#.', '.#.....#.', '..#...#..', '...###...', '...#.#...', '...#.#...',
                 '...#.#...', '...###...'),                     # a spanner: Fix a problem
    'settings': ('.#.......', '.#..####.', '###......', '.#.......', '......#..', '####.###.', '......#..',
                 '.........', '..#......'),                     # sliders: Settings
    'about': ('..#####..', '.#.....#.', '#...#...#', '#.......#', '#...#...#', '#...#...#', '#...#...#',
              '.#.....#.', '..#####..'),                        # an "i" in a ring: About
    'pilot': ('#########', '#.......#', '#.......#', '#########', '.........', '..#....#.', '.###.#...',
              '..#......', '.........'),                        # a small screen over a D-pad and A/B: Remote Pilot
    'log': ('######.', '#....#.', '#.##.#.', '#....#.', '#.##.#.', '#....#.', '######.'),
    # Private details, on an 11x9 grid: an open eye (showing) and the eye with a slash (hidden).
    'mask_off': ('...........', '....###....', '..##...##..', '.#...#...#.', '#...###...#', '.#...#...#.',
                 '..##...##..', '....###....', '...........'),
    'mask_on': ('.........#.', '....###.#..', '..##...#...', '.#...##..#.', '#...##....#', '.#..#....#.',
                '..##...##..', '..#.###....', '.#.........'),
    # Signs, 5 x 5, drawn dark on a small badge in the meaning's colour:
    # a tick (green, fine), "!" (yellow, attention or working), "x" (red, a problem), "i" (blue, plain information).
    'sign_ok': ('....#', '...##', '#.##.', '###..', '.#...'),
    'sign_warn': ('..#..', '..#..', '..#..', '.....', '..#..'),
    'sign_error': ('#...#', '.#.#.', '..#..', '.#.#.', '#...#'),
    'sign_info': ('..#..', '.....', '..#..', '..#..', '..#..'),
}
SIGN_TONES = {'ok': 'ok', 'update': 'warn', 'warn': 'warn', 'busy': 'warn', 'error': 'error', 'info': 'info',
              'dim': 'info'}


def draw_icon(painter, key, x, y, cell, colour):
    from PySide6.QtGui import QColor
    for row, line in enumerate(TAB_ICONS[key]):
        for col, ch in enumerate(line):
            if ch == '#':
                painter.fillRect(int(x + col * cell), int(y + row * cell), int(cell), int(cell), QColor(colour))


# The owner's own Chromatic (lime green, pink A/B, teal/mint corner stripes), from their photo.
CONSOLE_COLOURS = {'body': '#7cd33a', 'body_hi': '#a8ec6c', 'body_lo': '#57a826', 'edge': '#2a5a12',
                   'bezel': '#1a1a1a', 'lcd': '#1e9bff', 'lcd_hi': '#5cc4ff', 'lcd_edge': '#0a5fd0',
                   'ink': '#0a2a6e', 'grey': '#8a8a8a', 'red': '#f2cbc1', 'red_hi': '#fff2ec',
                   'stripe1': '#2f8f3a', 'stripe2': '#1d6e5e', 'dpad': '#141414',
                   'corner': ('#ffffff', '#ffffff', '#bdefd9', '#3aa593', '#1d6e5e', '#1d6e5e')}

# Our own screen pictures on the console's 10x9 LCD grid ('#' = ink).
SCREENS = {
    'info': ('..........', '..#....#..', '..#....#..', '..........', '..........', '..#....#..', '...####...',
             '..........', '..........'),        # our face: dot eyes, small smile
    'busy': ('..........', '..#....#..', '..#....#..', '..........', '..........', '..........',
             '.#..#..#..', '..........', '..........'),
    'ok': ('..........', '........#.', '.......##.', '......##..', '.#...##...', '.##.##....', '..###.....',
           '...#......', '..........'),
    'update': ('....##....', '...####...', '..######..', '.##.##.##.', '....##....', '....##....', '....##....',
               '....##....', '..........'),
    'error': ('....##....', '....##....', '...#..#...', '...####...', '..##..##..', '..##..##..', '.########.',
              '.###..###.', '##########'),
    # The guided Jellyfin sound setup: a small pixel Jellyfin mark (owner: "why not a
    # Jellyfin icon instead of this music note?"; '#' white outline, '*' purple inner triangle) and the
    # power sign (switch the console off and on).
    'sound': ('....##....', '...#..#...', '...#..#...', '..#.**.#..', '..#.**.#..', '.#.****.#.', '.#......#.',
              '##########', '..........'),
    'power': ('....##....', '.#..##..#.', '#...##...#', '#...##...#', '#........#', '#........#', '.#......#.',
              '..######..', '..........'),
    # The guided Pocket link setup: a link-cable plug (two prongs, the plug body, the cable).
    'link': ('..#....#..', '..#....#..', '.########.', '.#......#.', '.#......#.', '..#....#..', '...####...',
             '....##....', '....##....'),
}
INK_TONES = ('info', 'busy', 'sound', 'power', 'link')  # drawn in the screen's own dark ink

# The official Jellyfin logo (jellyfin/jellyfin-ux branding, icon-transparent.png, copyright the Jellyfin
# contributors, CC BY-SA 4.0: https://creativecommons.org/licenses/by-sa/4.0/), scaled to 18 x 18 pixels with
# its colours quantised; shown on the console sprite's screen during the Jellyfin sound setup (owner,
#). Notice: licenses/JELLYFIN-LOGO.txt.
JELLYFIN_MARK = (
    '........aa........', '.......aaab.......', '......cdaabb......', '.....cdaabbef.....',
    '.....gaa..bhi.....', '....cda....fjk....', '....da......il....', '...gda..bf..klk...',
    '..gda..beef..km...', '..cda..bfii..non..', '..da..bhffjk..no..', '.cpa..beiilk..noq.',
    '.aa...fiikkk...rr.', 'caa............qrs', 'aaab..........rrss', 'aabbbfikkknnnqrqst',
    'abbbiijlllooorrsss', '..bbfikkknnnqrrs..')
JELLYFIN_MARK_COLOURS = {
    'a': '#9060c0', 'b': '#7860c0', 'c': '#9048c0', 'd': '#a860c0', 'e': '#7878d8', 'f': '#6060c0', 'g': '#a848c0',
    'h': '#7878c0', 'i': '#6078c0', 'j': '#6078d8', 'k': '#4878c0', 'l': '#4878d8', 'm': '#4890d8', 'n': '#3078c0',
    'o': '#3090d8', 'p': '#a860d8', 'q': '#1890c0', 'r': '#1890d8', 's': '#0090d8', 't': '#00a8d8'}



def draw_console(painter, x, y, cell, tone='info'):
    """Our own pixel drawing of the Chromatic handheld (22 x 37 cells): the owner's green body, black
    bezel, blue screen showing our face or a status picture, D-pad, A/B, Start/Select and the
    corner stripe. Drawn in code, cell by cell."""
    from PySide6.QtGui import QColor
    c = CONSOLE_COLOURS

    def box(cx, cy, w, h, colour):
        painter.fillRect(int(x + cx * cell), int(y + cy * cell), int(w * cell), int(h * cell), QColor(colour))
    # body with a dark edge, lit top/left and shaded right/bottom, rounded corners
    box(1, 0, 20, 37, c['edge'])
    box(0, 1, 22, 35, c['edge'])
    box(1, 1, 20, 35, c['body'])
    box(1, 1, 20, 1, c['body_hi'])
    box(1, 1, 1, 34, c['body_hi'])
    box(20, 2, 1, 34, c['body_lo'])
    box(2, 35, 19, 1, c['body_lo'])
    # bezel and screen
    box(3, 3, 16, 15, c['bezel'])
    box(2, 4, 18, 13, c['bezel'])
    box(9, 4, 4, 1, c['grey'])           # speaker slot above the screen
    box(5, 6, 12, 9, c['lcd_edge'])
    box(6, 6, 11, 9, c['lcd'])
    box(6, 6, 11, 1, c['lcd_hi'])
    box(10, 16, 2, 1, c['grey'])         # small bar under the screen
    if tone == 'sound':  # the real Jellyfin mark at twice the screen's pixel resolution, on a dark screen
        box(5, 6, 12, 9, '#0c1424')
        for row, line in enumerate(JELLYFIN_MARK):
            for col, ch in enumerate(line):
                if ch != '.':
                    box(6 + 1 + col * 0.5, 6 + row * 0.5, 0.5, 0.5, JELLYFIN_MARK_COLOURS[ch])
    picture = () if tone == 'sound' else SCREENS.get(tone, SCREENS['info'])
    for row, line in enumerate(picture[:8]):
        for col, ch in enumerate(line):
            if ch in '#*' and 6 + col < 17:
                if tone == 'sound':  # the Jellyfin mark in its own colours
                    colour = '#ffffff' if ch == '#' else '#aa5cc3'
                else:
                    colour = (c['ink'] if tone in INK_TONES else
                              {'ok': '#0b5d00', 'update': '#7a5200', 'error': '#8a0000'}[tone])
                box(6 + col + 0.5, 6.5 + row, 1, 1, colour)
    # colour band under the screen
    box(2, 19, 5, 1, c['stripe1'])
    for n in range(5):
        box(8 + n * 2.4, 19, 1.6, 1, c['body_lo'])
    # D-pad with a light rim
    box(3, 24, 5, 1, c['body_hi'])
    box(4, 22, 3, 7, c['dpad'])
    box(2, 24, 7, 3, c['dpad'])
    box(5, 25, 1, 1, '#3a3a3a')
    # A and B
    for bx, by in ((17.5, 23), (14.5, 25)):
        box(bx, by, 2, 2, c['red'])
        box(bx, by, 1, 1, c['red_hi'])
        box(bx + 0.5, by + 2, 1.5, 0.6, c['body_lo'])
    # Select and Start (slanted pills)
    for sx in (7, 11):
        box(sx + 1, 30, 2, 1, '#2a2a2a')
        box(sx, 31, 2, 1, '#2a2a2a')
        box(sx + 0.5, 32, 2, 0.6, c['body_lo'])
    # corner paint: diagonal bands from the bottom-left corner inward (white, mint, teal, dark teal)
    for cy in range(29, 36):
        for cx in range(1, 8):
            d = (cx - 1) + (35 - cy)
            if d < len(c['corner']):
                box(cx, cy, 1, 1, c['corner'][d])


def icon_pixmap(size=256):
    """The app icon: our pixel console on a black tile (no ModRetro artwork)."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QPainter, QPixmap
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    p = QPainter(pixmap)
    p.fillRect(0, 0, size, size, QColor(PALETTE['window']))
    cell = size / 40
    draw_console(p, (size - 22 * cell) / 2, (size - 37 * cell) / 2, cell, 'ok')
    p.end()
    return pixmap


def apply(app, accent=None):
    """Style a QApplication (PySide6) with the Chromatic theme."""
    load_fonts()
    app.setFont(body_font())
    app.setStyleSheet(stylesheet(accent))
