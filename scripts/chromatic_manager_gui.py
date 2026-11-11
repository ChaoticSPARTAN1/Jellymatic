# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Chromatic Manager window: the same tool as `chromatic_manager.py`, with buttons.

Every action calls the unchanged Manager methods the text menu calls, inside the same
operating-system lock. Every write is approved by the person at this window in a plain
dialog that says what will be written, with APPROVE and CANCEL buttons (Cancel is the
default, Enter and Escape cancel); a version-changing restore asks a second time, saying
the stock version will change. Jobs run in the shared core
(`chromatic_manager_core.ManagerService`) on their own thread; the window shows the
manager's own messages and a progress bar. Nothing here writes to the console by itself:
each write starts from a button and that approval, or from a request made through the
local API (`chromatic_manager_api`, MCP, Remote Pilot) that the owner then approves in
this window; a program can never press Approve.

AI control is the owner's opt-in: on the first AI request of each run, a pop-up asks
whether the AI may drive the app (Allow / Don't allow, "Always remember this"). Only a
click in this window answers it; the Settings tab shows and clears the remembered choice.
Allowed, a request starts without a per-change Approve, after the project's hardware usage
check, with every safety check of the manager; the window shows what the AI does, and Stop
takes the permission back at once.

The Home tab shows one plain status ("Up to date", "Update available", or a problem) and at
most one main button. When a console is plugged in while the window is open, it is checked
by itself, read-only (about 20 seconds). The window never shows or stores Wi-Fi details,
device identifiers or flash contents; it only shows what the manager says, which never
names them.

The Remote Pilot tab starts Remote Pilot (its own folder, found or chosen once; see
remote_pilot_link.py) and opens its Chromatic page in a window of its own. Before every job
the window's service asks Remote Pilot to let go of the console's USB cable and gives it back
afterwards (the USB hand-off); a sign at the top says so while it is paused.

Run from source:  Chromatic-Manager-Window.cmd (or python scripts/chromatic_manager_gui.py in a venv with requirements.txt)
The portable build (scripts/Build-ChromaticManagerApp.ps1) runs this same file.
"""
from __future__ import annotations

import argparse
from collections import deque
import json
import math
import os
import re
from pathlib import Path
import sys
import threading
import time

from PySide6.QtCore import QEvent, QObject, QPointF, QRect, QRectF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (QApplication, QCheckBox, QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
                               QMessageBox, QPlainTextEdit, QPushButton, QSizePolicy, QStackedWidget, QStyle,
                               QStyleOptionButton, QToolTip, QVBoxLayout, QWidget)

import chromatic_manager_owner as owner_actions
import chromatic_theme as theme
import remote_pilot_link as pilot_link
from chromatic_manager_core import (ACTIONS, AI_GRANT, API_SUFFIX, APPROVED_IN_WINDOW, BANNERS, BY_KEY, FLASH_SIZE,  # noqa: F401
                                    LOG_FOLDER, MODE_NAMES, OWNER, QUICK_ANSWERS, READ_RATE, REASON_MAX, RESULTS,
                                    ROW_TEXT, SOUND_FPGA, SOUND_GUIDE_FILE, SOUND_MINUTES, SOUND_STEPS, SoundGuide,
                                    GUIDE_ONLY_KEYS, LINK_FPGA, LINK_GUIDE_FILE, LinkGuide, link_answers_good, link_state,
                                    LINK_UPDATE_KEY, OWNER_APPROVE_KEYS, STARTED_IN_WINDOW, LinkUpdateGuide,
                                    built_sound_version, short_release, sound_state,
                                    JELLYFIN_PAGE, mask_name, setup_sign, sound_after_update,
                                    STEP_NAMES, WRITE_KEYS, Action, ManagerService, ObservedDevice, Progress, RunLog,
                                    Tracker, clock_text, mask_private, observe, quick_answers, redact, run_action,
                                    summarize,
                                    what_next, guide, what_to_do)


ROOT = Path(__file__).resolve().parents[1]
from user_paths import PRIVATE  # noqa: E402  (your per-user Jellymatic folder; see user_paths.py)


def data_folder(root):
    """Where the window keeps its own files: your per-user Jellymatic folder for the real program
    folder, or the folder a test or preview passes instead."""
    return PRIVATE if Path(root).resolve() == ROOT.resolve() else Path(root)


APP_NAME = 'Chromatic Manager'
WINDOW = theme.GEOMETRY['window']          # compact like the official updater; the smallest size, resizable
WINDOW_MIN = WINDOW
WINDOW_LOG = theme.GEOMETRY['window_log']  # the same window with the log panel open


class Bridge(QObject):
    """Carries the service's events to the window's thread (queued when they come from
    the job or the local API, direct when the window itself started them)."""
    event = Signal(str, dict)
    question = Signal(str, bool)
    pilot = Signal(str, object)  # Remote Pilot tab: work done on a helper thread, back on the window's thread


# ---- private details: masked on screen by default ----------------------------------------
class Privacy:
    """Whether the window masks private details (on at every start,, so a
    screenshot never shows them). Display only: the jobs, the log files and the API keep the
    real text; only what the window draws changes. Widgets that show changing text keep the
    real text and re-draw it when the choice changes (see set)."""
    on = True

    @classmethod
    def shown(cls, text):
        text = '' if text is None else str(text)
        return mask_private(text) if cls.on else text

    @classmethod
    def set(cls, on):
        cls.on = bool(on)
        app = QApplication.instance()
        for widget in (app.allWidgets() if app else ()):
            refresh = getattr(widget, 'refresh_mask', None)
            if callable(refresh):
                refresh()


shown = Privacy.shown

PRIVACY_TIP_ON = ('Private details are hidden. Addresses, network names, device IDs, user names, tokens and private '
                  'folders show as ****, so a screenshot is safe to share. Click to show them. Only the screen '
                  'changes; jobs and log files stay as they are.')
PRIVACY_TIP_OFF = ('Private details are showing. Click to hide them (shown as ****) before you take a screenshot. '
                   'They are hidden again each time this app starts.')


class TipMask(QObject):
    """Shows every tooltip of the application masked while private details are hidden."""

    def eventFilter(self, obj, event):
        if (event.type() == QEvent.Type.ToolTip and Privacy.on and isinstance(obj, QWidget) and obj.toolTip()
                and shown(obj.toolTip()) != obj.toolTip()):
            QToolTip.showText(event.globalPos(), shown(obj.toolTip()), obj)
            return True
        return False


def shown_tooltip(widget):
    """The tooltip as the window shows it."""
    return shown(widget.toolTip())


class MaskedLabel(QLabel):
    """A label that keeps the real text and shows it masked while private details are hidden.
    text() is what is on screen; raw_text() the real text."""

    def __init__(self, text=''):
        super().__init__()
        self.setText(text)

    def setText(self, text):
        self._raw_text = '' if text is None else str(text)
        super().setText(shown(self._raw_text))

    def raw_text(self):
        return self._raw_text

    def refresh_mask(self):
        super().setText(shown(self._raw_text))


# ---- widgets -------------------------------------------------------------------------------
def font_of(widget, size=None, bold=None):
    font = QFont(widget.font())
    font.setStyleStrategy(QFont.StyleStrategy.NoAntialias)
    if size:
        font.setPointSizeF(size)
    if bold is not None:
        font.setBold(bold)
    return font


def colour(name):
    return QColor(theme.PALETTE.get(name, name))


def bevel(p, rect, face, hi, shade, outline=None, pressed=False):
    """A pixel button or panel: dark outline with cut corners, a lit top/left edge and a
    shaded bottom/right edge (swapped while pressed)."""
    x, y, w, h = rect.x(), rect.y(), rect.width(), rect.height()
    o = colour(outline or 'outline')
    p.fillRect(x + 2, y, w - 4, h, o)
    p.fillRect(x, y + 2, w, h - 4, o)
    p.fillRect(x + 1, y + 1, w - 2, h - 2, o)
    p.fillRect(x + 2, y + 2, w - 4, h - 4, colour(face))
    top, bottom = (shade, hi) if pressed else (hi, shade)
    p.fillRect(x + 2, y + 2, w - 4, 2, colour(top))
    p.fillRect(x + 2, y + 2, 2, h - 4, colour(top))
    p.fillRect(x + 2, y + h - 5, w - 4, 3, colour(bottom))
    p.fillRect(x + w - 5, y + 2, 3, h - 4, colour(bottom))


class FitButton(QPushButton):
    """A pixel button whose text always fits it: drawn at the largest size (down to MIN_PT)
    that fits on one line, else on two lines. Kinds: '' / 'no' grey, 'go' / 'ok' / 'yes'
    green, 'stop' / 'error' red, 'update' amber, 'link' (green underlined text), 'bottom' (an
    icon and white text), tabs (property tab, icon only) and 'close' (the red close box)."""
    MIN_PT = 6  # Silkscreen (the body pixel font) is wide: "NEW JELLYFIN" needs 6 pt in the narrowest button
    FACES = {'': ('button', 'button_hi', 'button_shade', 'outline'),
             'go': ('button_go', 'button_go_hi', 'button_go_shade', 'outline'),
             'stop': ('button_stop', 'button_stop_hi', 'button_stop_shade', 'outline'),
             'update': ('button_warn', 'button_warn_hi', 'button_warn_shade', 'outline')}
    FACE_OF = {'no': '', 'ok': 'go', 'yes': 'go', 'error': 'stop'}

    def __init__(self, text='', kind='', size=None):
        super().__init__(text)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        if kind:
            self.setProperty('kind', kind)
        self.base_pt = size or theme.GEOMETRY['base_point_size']
        self.icon_key, self.tab_colour = None, None
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.setFont(theme.body_font(self.base_pt, True))

    def kind(self):
        return self.property('kind') or ''

    def _contents(self, rect=None):
        r = rect or self.rect()
        if self.kind() in ('link', 'bottom'):
            left = 18 if self.kind() == 'bottom' else 2
            return r.adjusted(left, 0, -2, 0)
        return r.adjusted(9, 4, -9, -6)

    def _font(self, size):
        font = theme.body_font(size, self.kind() not in ('link',))
        font.setUnderline(self.kind() == 'link')
        return font

    def shown_text(self):
        return self.text() if self.kind() in ('link', 'bottom') else self.text().upper()

    def layout_text(self, rect=None):
        """(font, text with at most one line break) that fits `rect` (default: the button)."""
        area = self._contents(rect)
        text = self.shown_text()
        size = self.base_pt
        while size >= self.MIN_PT:
            metrics = QFontMetrics(self._font(size))
            if metrics.horizontalAdvance(text) <= area.width() and metrics.height() <= area.height() + 4:
                return self._font(size), text
            size -= 0.5
        words = text.split(' ')
        size = self.base_pt
        while size >= self.MIN_PT and len(words) > 1:
            metrics = QFontMetrics(self._font(size))
            best = min((' '.join(words[:i]) + '\n' + ' '.join(words[i:]) for i in range(1, len(words))),
                       key=lambda t: max(metrics.horizontalAdvance(part) for part in t.split('\n')))
            if (max(metrics.horizontalAdvance(part) for part in best.split('\n')) <= area.width()
                    and 2 * metrics.height() <= area.height() + 6):
                return self._font(size), best
            size -= 0.5
        return self._font(self.MIN_PT), text

    def text_fits(self):
        """Does the text, as drawn, fit inside the button's contents?"""
        if not self.text():
            return True
        font, text = self.layout_text()
        metrics, area = QFontMetrics(font), self._contents()
        lines = text.split('\n')
        return (max(metrics.horizontalAdvance(line) for line in lines) <= area.width()
                and len(lines) * metrics.height() <= area.height() + 6)

    def sizeHint(self):
        metrics = QFontMetrics(self._font(self.base_pt))
        if self.kind() in ('link', 'bottom'):
            return QSize(metrics.horizontalAdvance(self.shown_text()) + (24 if self.kind() == 'bottom' else 6),
                         metrics.height() + 4)
        return QSize(max(metrics.horizontalAdvance(self.shown_text()) + 30, 64), metrics.height() + 16)

    def minimumSizeHint(self):
        return QSize(48 if self.kind() not in ('link', 'bottom') else 30, self.sizeHint().height())

    def text_colour(self):
        c = theme.PALETTE
        if not self.isEnabled():
            return QColor(c['text_faint'] if self.kind() in ('link', 'bottom') else '#6a6a6a')
        if self.kind() == 'link':
            return QColor(c['text'] if self.underMouse() else c['link'])
        if self.kind() == 'bottom':
            return QColor(c['text'])
        face = self.FACE_OF.get(self.kind(), self.kind())
        return QColor('#ffffff' if face == 'stop' else '#000000')

    def paintEvent(self, event):
        p = QPainter(self)
        r = self.rect()
        if self.property('tab'):
            self._paint_tab(p, r)
            p.end()
            return
        kind = self.kind()
        if kind == 'close':
            p.fillRect(r, colour('outline'))
            p.fillRect(r.adjusted(2, 2, -2, -2), colour('button_stop'))
            cell = max(2, (r.width() - 6) // 7)
            ox, oy = r.x() + (r.width() - 7 * cell) // 2, r.y() + (r.height() - 7 * cell) // 2
            for i in range(7):
                for j in (i, 6 - i):
                    p.fillRect(ox + j * cell, oy + i * cell, cell, cell, QColor('#ffffff'))
            p.end()
            return
        if kind == 'privacy':  # a bevelled grey pixel button with our eye icon (with a slash: hidden)
            bevel(p, r, *self.FACES[''], pressed=self.isDown())
            cell = 2
            key = 'mask_on' if Privacy.on else 'mask_off'
            w, h = len(theme.TAB_ICONS[key][0]) * cell, len(theme.TAB_ICONS[key]) * cell
            dx = 1 if self.isDown() else 0
            theme.draw_icon(p, key, r.x() + (r.width() - w) // 2 - 1 + dx, r.y() + (r.height() - h) // 2 - 1 + dx,
                            cell, theme.PALETTE['outline'])
            p.end()
            return
        if kind == 'bottom' and self.icon_key:
            theme.draw_icon(p, self.icon_key, r.x() + 2, r.y() + (r.height() - 14) // 2, 2, theme.PALETTE['warn'])
        elif kind not in ('link', 'bottom'):
            face = self.FACE_OF.get(kind, kind)
            names = self.FACES.get(face, self.FACES[''])
            if not self.isEnabled():
                names = ('#3a3a3a', '#4a4a4a', '#262626', 'outline')
            elif self.underMouse() and not self.isDown():
                p.setOpacity(1.0)
            bevel(p, r, *names, pressed=self.isDown())
        font, text = self.layout_text()
        p.setFont(font)
        p.setPen(self.text_colour())
        area = self._contents()
        if self.isDown() and kind not in ('link', 'bottom'):
            area = area.translated(1, 1)
        align = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter if kind in ('link', 'bottom') else \
            Qt.AlignmentFlag.AlignCenter
        p.drawText(area, int(align), text)
        p.end()

    def _paint_tab(self, p, r):
        """A rounded pixel tab with our own icon: the open tab in its colour, the rest grey."""
        on = self.isChecked()
        edge = colour(self.tab_colour if on else 'tab_idle')
        x, y, w, h = r.x(), r.y(), r.width(), r.height()
        p.fillRect(x + 4, y, w - 8, 3, edge)
        p.fillRect(x + 2, y + 1, w - 4, 3, edge)
        p.fillRect(x, y + 4, w, h - 4, edge)
        p.fillRect(x + 4, y + 3, w - 8, h - 3, colour('window' if on else 'tab_idle_fill'))
        p.fillRect(x + 3, y + 5, w - 6, h - 5, colour('window' if on else 'tab_idle_fill'))
        cell = 2
        icon_colour = theme.PALETTE['text'] if on else theme.PALETTE['tab_idle']
        if not self.isEnabled() and not on:
            icon_colour = '#3e3e3e'
        theme.draw_icon(p, self.icon_key, x + (w - 9 * cell) // 2, y + (h - 9 * cell) // 2 + 1, cell, icon_colour)


class SegmentBar(QWidget):
    """The updater's 13-segment progress bar: 17-wide segments, 20 apart, green when lit."""

    def __init__(self):
        super().__init__()
        g = theme.GEOMETRY
        self.segments, self.step = g['progress_segments'], g['progress_step']
        w, h = g['progress_segment']
        self.size_ = QSize(self.step * (self.segments - 1) + w + 6, h)
        self.setFixedSize(self.size_)
        self.lit, self.busy, self.tick = 0, False, 0
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._spin)

    def set_percent(self, percent, moving=True):
        """None: no count is known, so a lit pair runs along the bar. A number lights that
        share, and while `moving` the next segment blinks, so the bar never looks stuck.
        The timer runs on the window's thread, so it keeps going while the worker waits."""
        busy = percent is None
        if not busy:
            self.lit = max(0, min(self.segments, int(percent * self.segments / 100)))
        interval = 120 if busy else 450 if moving and self.lit < self.segments else 0
        if busy != self.busy or not self.timer.isActive() or self.timer.interval() != interval:
            self.busy = busy
            if interval:
                self.timer.start(interval)
            else:
                self.timer.stop()
        self.update()

    def _spin(self):
        self.tick = (self.tick + 1) % self.segments
        self.update()

    def paintEvent(self, event):
        p, c = QPainter(self), theme.PALETTE
        w, h = theme.GEOMETRY['progress_segment']
        blink = self.timer.isActive() and self.tick % 2 == 0
        for i in range(self.segments):
            on = ((i == self.tick or i == (self.tick + 1) % self.segments) if self.busy else
                  i < self.lit or (i == self.lit and blink))
            hi, mid, lo = ((c['progress_hi'], c['progress_mid'], c['progress_lo']) if on else
                           (c['progress_off_hi'], c['progress_off_mid'], c['progress_off_lo']))
            x = 3 + i * self.step
            p.fillRect(QRect(x, 0, w, h), QColor(lo))
            p.fillRect(QRect(x, 0, w, h // 2), QColor(mid))
            p.fillRect(QRect(x, 0, w, 2), QColor(hi))
        p.end()


class StepStrip(QWidget):
    """The sound setup's progress: one pixel block per step in the progress bar's colours,
    green for done, amber (blinking-free) for the step on screen, dim for the steps to come."""
    W, H, GAP = 22, 12, 5

    def __init__(self, steps=5):
        super().__init__()
        self.steps, self.current = steps, 0
        self.setFixedSize(steps * (self.W + self.GAP) - self.GAP + 2, self.H)

    def set_step(self, step):
        self.current = step
        self.update()

    def paintEvent(self, event):
        p, c = QPainter(self), theme.PALETTE
        for i in range(1, self.steps + 1):
            if i < self.current or self.current == 0:
                hi, mid, lo = c['progress_hi'], c['progress_mid'], c['progress_lo']
            elif i == self.current:
                hi, mid, lo = c['button_warn_hi'], c['button_warn'], c['button_warn_shade']
            else:
                hi, mid, lo = c['progress_off_hi'], c['progress_off_mid'], c['progress_off_lo']
            x = 1 + (i - 1) * (self.W + self.GAP)
            p.fillRect(QRect(x, 0, self.W, self.H), QColor(lo))
            p.fillRect(QRect(x, 0, self.W, self.H // 2), QColor(mid))
            p.fillRect(QRect(x, 0, self.W, 2), QColor(hi))
        p.end()


class PilotSteps(QWidget):
    """The Remote Pilot tab's three steps (start, find the console, connected), drawn like the
    progress bar's segments: green done, amber now, red where it stopped, dim to come; each
    with its name under it, white for the step on screen."""
    NAMES = ('Start', 'Find console', 'Connected')
    W, H, GAP = 92, 12, 10

    def __init__(self):
        super().__init__()
        self.states = ['todo'] * 3
        self.setFixedSize(3 * self.W + 2 * self.GAP + 2, self.H + 18)
        self.setToolTip('The three steps to using Remote Pilot: it starts on this PC, it finds your Chromatic, and '
                        'it is connected. Green is done, yellow is happening now, red needs you.')

    def set_states(self, states):
        self.states = list(states)
        self.update()

    def paintEvent(self, event):
        p, c = QPainter(self), theme.PALETTE
        faces = {'done': ('progress_hi', 'progress_mid', 'progress_lo'),
                 'now': ('button_warn_hi', 'button_warn', 'button_warn_shade'),
                 'fail': ('button_stop_hi', 'button_stop', 'button_stop_shade'),
                 'todo': ('progress_off_hi', 'progress_off_mid', 'progress_off_lo')}
        p.setFont(theme.body_font(theme.GEOMETRY['small_point_size'] - 1))
        for i, (state, name) in enumerate(zip(self.states, self.NAMES)):
            hi, mid, lo = (QColor(c[key]) for key in faces[state])
            x = 1 + i * (self.W + self.GAP)
            p.fillRect(QRect(x, 0, self.W, self.H), lo)
            p.fillRect(QRect(x, 0, self.W, self.H // 2), mid)
            p.fillRect(QRect(x, 0, self.W, 2), hi)
            p.setPen(QColor(c['text'] if state in ('now', 'fail') else c['text_dim'] if state == 'done'
                            else c['text_faint']))
            p.drawText(QRect(x - 4, self.H + 2, self.W + 8, 16), int(Qt.AlignmentFlag.AlignCenter), name.upper())
        p.end()


class PixelText(QWidget):
    """Text drawn as chunky pixels (the header title, headlines, the logo): see
    theme.pixel_text_image. With `wrap`, words move to further lines rather than the text
    ever being cut or squeezed; `centre` centres each line."""

    def __init__(self, text='', colours='#000000', scale=3, base_px=8, wrap=False, centre=False):
        super().__init__()
        self._raw, self.colours, self.scale, self.base_px = text, colours, scale, base_px
        self._text = shown(text)
        self.wrap, self.centre = wrap, centre
        self.setSizePolicy(QSizePolicy.Policy.Preferred,
                           QSizePolicy.Policy.Preferred if wrap else QSizePolicy.Policy.Fixed)
        self._render()

    def text(self):
        """The text as drawn (masked while private details are hidden); raw_text(): the real text."""
        return self._text

    def raw_text(self):
        return self._raw

    def setText(self, text):
        self._raw = '' if text is None else str(text)
        self._text = shown(self._raw)
        self._render()

    def refresh_mask(self):
        if shown(self._raw) != self._text:
            self._text = shown(self._raw)
            self._render()

    def set_colours(self, colours):
        self.colours = colours
        self._render()

    def _image_of(self, text):
        return theme.pixel_text_image(text, self.colours, self.scale, self.base_px)

    def _render(self):
        self._image = self._image_of(self._text)
        self._line_h = self._image.height()
        if not self.wrap:
            self.setFixedHeight(self._line_h)
        self.updateGeometry()
        self.update()

    def lines(self, width):
        """The text split into lines that each fit `width` pixels."""
        if not self.wrap or self._image.width() <= width or not self._text:
            return [self._text]
        lines, current = [], ''
        for word in self._text.split(' '):
            trial = f'{current} {word}'.strip()
            if current and self._image_of(trial).width() > width:
                lines.append(current)
                current = word
            else:
                current = trial
        return lines + [current]

    def hasHeightForWidth(self):
        return self.wrap

    def heightForWidth(self, width):
        return len(self.lines(width)) * self._line_h if self._text else 0

    def sizeHint(self):
        return QSize(min(self._image.width(), 460) if self.wrap else self._image.width(),
                     self.heightForWidth(460) if self.wrap else self._line_h)

    def minimumSizeHint(self):
        return QSize(10, self._line_h if self._text else 0)

    def paintEvent(self, event):
        p = QPainter(self)
        y = 0
        for line in self.lines(self.width()):
            image = self._image_of(line) if line != self._text else self._image
            if image.width() > self.width():  # one word wider than the space: shrink, never cut
                image = image.scaled(self.width(), image.height(), Qt.AspectRatioMode.KeepAspectRatio,
                                     Qt.TransformationMode.FastTransformation)
            x = (self.width() - image.width()) // 2 if self.centre else 0
            p.drawImage(x, y + (self._line_h - image.height()) // 2 if not self.wrap else y, image)
            y += self._line_h
        p.end()


class ConsoleArt(QWidget):
    """Our own pixel drawing of the Chromatic; its screen shows the status (theme.draw_console)."""
    CELL = 4

    def __init__(self):
        super().__init__()
        self.tone = 'info'
        self.setFixedSize(22 * self.CELL + 4, 37 * self.CELL + 4)
        self.setToolTip('Your Chromatic. Its screen shows how it is: a tick when it is up to date, an arrow '
                        'when an update is ready, a warning sign when something needs fixing.')

    def set_tone(self, tone):
        self.tone = tone if tone in theme.SCREENS else 'info'
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        theme.draw_console(p, 2, 2, self.CELL, self.tone)
        p.end()


class StatusCard(QWidget):
    """One plain status, centred in the speech bubble: a headline in the tone's colour and
    a line or two under it. The console drawing next to the bubble shows the same tone."""

    def __init__(self, headline_pt=14, pixel=True):
        super().__init__()
        self.pixel = pixel
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        layout = QVBoxLayout(self)
        layout.setSizeConstraint(QVBoxLayout.SizeConstraint.SetMinimumSize)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.head = PixelHead('', TONE_INK['info'], 2, wrap=True, centre=True) if pixel else label('', 'info')
        if not pixel:
            self.head.setFont(theme.heading_font(headline_pt))
            self.head.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.sub = label('', 'notice', size=theme.GEOMETRY['base_point_size'])
        self.sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.head)
        layout.addWidget(self.sub)
        self.tone = 'info'
        self.on_tone = None

    def set(self, tone, headline, sub='', tooltip=''):
        self.tone = tone
        self.head.setProperty('role', tone)
        if self.pixel:
            self.head.colours = TONE_INK.get(tone, TONE_INK['info'])
        self.head.setText(headline)
        self.sub.setText(sub)
        self.sub.setVisible(bool(sub))
        self.setToolTip(tooltip)
        restyle(self.head)
        if self.on_tone:
            self.on_tone(tone)


TONE_INK = {'ok': theme.PALETTE['ok'], 'update': theme.PALETTE['warn'], 'error': theme.PALETTE['error'],
            'info': theme.PALETTE['text'], 'busy': theme.PALETTE['warn']}


class PixelHead(PixelText):
    """A headline in pixel letters that also carries a style role (like a QLabel)."""


def label(text='', role=None, wrap=True, size=None, bold=None):
    widget = MaskedLabel(text)
    widget.setWordWrap(wrap)
    if role:
        widget.setProperty('role', role)
    widget.setFont(theme.body_font(size or theme.GEOMETRY['base_point_size'], bool(bold)))
    return widget


def button(text, kind='', size=None):
    return FitButton(text, kind, size)


def restyle(widget):
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()


def program_name(requested_by):
    """'Claude Code via MCP [program via local API]' -> 'Claude Code via MCP'."""
    text = str(requested_by or '')
    return text[:-len(API_SUFFIX)] if text.endswith(API_SUFFIX) else text


LOGO_COLOURS = ('#ff4d4d', '#ff9a3c', '#ffd93c', '#7ee05a', '#3cc8ff', '#8f7bff', '#ff6ad5')
CREDIT = 'Created by ChaoticSPARTAN1'
CREDIT_PT = theme.GEOMETRY['small_point_size'] - 1.5  # 6.5 pt: subtle, below the buttons' 8-9 pt, in the body pixel font


class Bubble(QWidget):
    """The black speech-bubble panel with a grey bevelled border and a small pointer toward
    the console on its left."""
    POINTER = 12

    def __init__(self):
        super().__init__()
        self.setContentsMargins(self.POINTER + 14, 12, 18, 14)

    def paintEvent(self, event):
        p = QPainter(self)
        r = self.rect().adjusted(self.POINTER, 0, 0, 0)
        p.fillRect(r.adjusted(5, 5, 0, 0), colour('frame_shadow'))           # drop shadow right/bottom
        p.fillRect(r.adjusted(0, 0, -5, -5), colour('frame_dim'))
        p.fillRect(r.adjusted(0, 0, -5, -5).adjusted(3, 3, -3, -3), colour('window'))
        p.fillRect(QRect(r.x(), r.y(), r.width() - 5, 1), colour('frame_light'))
        # the pointer: a stepped triangle toward the console
        mid = r.y() + int(r.height() * 0.62)
        for i in range(self.POINTER):
            half = self.POINTER - i
            p.fillRect(r.x() - self.POINTER + i, mid - half // 2, 1, half, colour('frame_dim'))
        p.end()


# ---- plain wording for the manager's questions ------------------------------------------------
# The manager's own words stay as they are (the text menu and its tests use them); the window
# shows these instead. First match wins.
PLAIN_PROMPTS = (
    ('Use this PC at ', 'Wi-Fi setup',
     lambda p: 'Use this PC ({}) to bring Jellyfin to your Chromatic?'.format(
         (re.match(r'Use this PC at (\S+) for', p) or re.match(r'(.*)', '')).group(1) or 'this PC')),
    ("This PC's LAN address", 'Wi-Fi setup',
     lambda p: "Type this PC's address on your home network (for example 192.0.2.20)."),
    ('Wi-Fi network name', 'Wi-Fi setup',
     lambda p: 'The Wi-Fi network to add, or to give a new password. If you have two, use the 2.4 GHz one. The '
               'networks already on your Chromatic stay. Leave it empty to only change their order or remove some.'),
    ('Wi-Fi networks the console will have', 'Wi-Fi setup',
     lambda p: 'Type ok to write the list as it is, or the numbers in a new order (a number left out is removed).'),
    ('Wi-Fi password', 'Wi-Fi setup', lambda p: 'Your Wi-Fi password. Leave it empty if your Wi-Fi has none.'),
    ('Type done once', 'Official update',
     lambda p: "Run ModRetro's updater now. When it says it worked, close it and press Done. If it failed, "
               'press It failed. Stop here keeps the start-up menu off.'),
    ("The console's firmware did not change", 'Official update',
     lambda p: "Your Chromatic's firmware did not change. Did ModRetro's updater say it installed an update?"),
)
ANSWER_LABELS = {'done': 'Done', 'failed': 'It failed', 'cancel': 'Stop here', 'yes': 'Yes', 'no': 'No'}


def plain_prompt(prompt):
    """(title, plain text) for a question from a running job."""
    for start, title, text in PLAIN_PROMPTS:
        if str(prompt).startswith(start):
            return title, text(str(prompt))
    return APP_NAME, str(prompt).strip()


def fit_dialog(dialog, parent, width=520):
    """Keep a dialog inside the window, whatever the window's size."""
    if parent is not None and parent.isVisible():
        width = min(width, max(320, parent.width() - 40))
    dialog.setFixedWidth(width)


def centre_on(dialog, parent):
    if parent is not None and parent.isVisible():
        dialog.adjustSize()
        frame = parent.frameGeometry()
        dialog.move(frame.center().x() - dialog.width() // 2, frame.center().y() - dialog.height() // 2)


class AskDialog(QDialog):
    """A question from a running job, in plain words. Ready-made answers are buttons; a
    typed answer goes in a field (hidden for a password).

    Keystrokes meant for another window must not answer it: there is no default button
    (Enter does nothing), and the field and buttons ignore input for the first second."""
    SETTLE_MS = 1000

    def __init__(self, parent, prompt, secret=False, confirm_word=None):
        super().__init__(parent)
        title, text = plain_prompt(prompt)
        self.setWindowTitle(title)
        self.setModal(True)
        fit_dialog(self, parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(14)
        layout.addWidget(label(title, 'info', size=theme.GEOMETRY['base_point_size'] + 3, bold=True))
        self.text = label(text, 'value', size=theme.GEOMETRY['base_point_size'] + 1)
        layout.addWidget(self.text)
        answers = quick_answers(prompt)
        self.field = QLineEdit()
        if secret:
            self.field.setEchoMode(QLineEdit.EchoMode.Password)
            self.field.setToolTip('What you type is hidden and never saved in a log.')
        self.field.setVisible(secret or not answers)
        layout.addWidget(self.field)
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addStretch(1)
        self.controls = [self.field]
        for answer in answers:
            quick = button(ANSWER_LABELS.get(answer, answer.capitalize()), 'yes' if answer in ('done', 'yes') else 'no')
            quick.clicked.connect(lambda _=False, a=answer: self._finish(a))
            row.addWidget(quick)
            self.controls.append(quick)
        if confirm_word:
            cancel = button('Cancel', 'no')
            cancel.clicked.connect(self.reject)
            row.addWidget(cancel)
        self.ok = button('OK', 'yes')
        self.ok.clicked.connect(lambda: self._finish(self.field.text()))
        self.ok.setVisible(not self.field.isHidden())
        row.addWidget(self.ok)
        self.controls.append(self.ok)
        layout.addLayout(row)
        for widget in self.findChildren(QPushButton):
            widget.setAutoDefault(False)
            widget.setDefault(False)
        self.answer = ''
        for widget in self.controls:
            widget.setEnabled(False)
        centre_on(self, parent)
        QTimer.singleShot(self.SETTLE_MS, self._settle)

    def _settle(self):
        for widget in self.controls:
            widget.setEnabled(True)
        self.field.setFocus()

    def _finish(self, text):
        self.answer = text
        self.accept()


class ApproveDialog(QDialog):
    """A plain approval: what will happen, then APPROVE or CANCEL. Only a click on APPROVE
    (or Tab to it and Space) approves. CANCEL is the default button, so Enter cancels, and
    Escape or closing the dialog cancels too. As with AskDialog, APPROVE ignores input for
    the first second, so a click or keystroke meant for another window cannot approve.
    `headline`: a line in big pixel letters above the text (the one-step update's "The new image
    is running now"); `cancel_text`: what the button that changes nothing is called there."""
    SETTLE_MS = AskDialog.SETTLE_MS

    def __init__(self, parent, text, approve_text='Approve', headline=None, cancel_text='Cancel'):
        super().__init__(parent)
        self.setWindowTitle(APP_NAME)
        self.setModal(True)
        fit_dialog(self, parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(14)
        self.head = None
        if headline:
            self.head = PixelHead(headline, TONE_INK['update'], 3, wrap=True, centre=True)
            self.head.setMinimumHeight(self.head.heightForWidth(self.width() - 44))
            layout.addWidget(self.head)
        said = label(text, 'value', size=theme.GEOMETRY['base_point_size'] + 1)
        # The wrapped text's full height at the dialog's fixed width: without it the layout sized the
        # label for an unwrapped width and cut the last lines off (the image to write, Keep plugged in).
        said.setMinimumHeight(said.heightForWidth(self.width() - 44))
        layout.addWidget(said)
        row = QHBoxLayout()
        row.addStretch(1)
        self.cancel = button(cancel_text, 'no')
        self.cancel.setToolTip('Does nothing. Enter and Esc also cancel.' if cancel_text == 'Cancel' else
                               'Writes nothing. Enter, Esc and closing this do the same.')
        self.cancel.clicked.connect(self.reject)
        self.approve = button(approve_text, 'yes')
        self.approve.clicked.connect(self._approved)
        row.addWidget(self.cancel)
        row.addWidget(self.approve)
        layout.addLayout(row)
        self.approve.setAutoDefault(False)
        self.approve.setDefault(False)
        self.cancel.setAutoDefault(True)
        self.cancel.setDefault(True)
        self.approved = False
        self.approve.setEnabled(False)
        self.cancel.setFocus()
        centre_on(self, parent)
        QTimer.singleShot(self.SETTLE_MS, lambda: self.approve.setEnabled(True))

    def _approved(self):
        self.approved = True
        self.accept()

    def reject(self):
        self.approved = False
        super().reject()


class AiControlDialog(QDialog):
    """The owner's opt-in for AI control, shown on the first AI request of a run. ALLOW lets
    the AI install updates and use the whole app without a per-change Approve; DON'T ALLOW
    (the default: Enter, Escape and closing choose it) keeps an Approve for every change.
    "Always remember this" keeps the answer in the app's private settings. As with the
    Approve dialog, ALLOW ignores input for its first second."""
    SETTLE_MS = AskDialog.SETTLE_MS

    def __init__(self, parent, requested_by, action=None, image=None):
        super().__init__(parent)
        self.setWindowTitle('Let the AI help?')
        self.setModal(True)
        fit_dialog(self, parent, 540)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(12)
        layout.addWidget(label('Let the AI use Chromatic Manager?', 'info', size=theme.GEOMETRY['base_point_size'] + 4,
                               bold=True))
        who = program_name(requested_by) or 'A program on this PC'
        asks = f' It asks to: {action.title}.' if action is not None else ''
        layout.addWidget(label(f'{who} (a program on this PC) wants to install updates and use this app for '
                               f'you.{asks}', 'value', size=theme.GEOMETRY['base_point_size'] + 1))
        self.image = label(image_line(image), 'update', bold=True)
        self.image.setToolTip(IMAGE_TIP)
        layout.addWidget(self.image)
        self.body = label('If you allow it, it can check, install and fix things without asking each time. '
                          'The same safety checks run, you see what it does here, and you can press Stop at any '
                          'time. It never sees your Wi-Fi passwords, Jellyfin sign-in or backups.', 'dim')
        self.body.setToolTip('Until you allow it, you approve every change yourself. The AI also has to pass the '
                             'project\'s usage check before it changes anything.')
        layout.addWidget(self.body)
        self.remember = QCheckBox('Always remember this')
        self.remember.setToolTip('Keeps your answer on this PC. You can clear it any time on the Settings tab.')
        layout.addWidget(self.remember)
        row = QHBoxLayout()
        row.addStretch(1)
        self.deny = button("Don't allow", 'no')
        self.deny.setToolTip('You approve every change yourself. Enter and Esc also choose this.')
        self.deny.clicked.connect(self._denied)
        self.allow = button('Allow', 'yes')
        self.allow.clicked.connect(self._allowed)
        row.addWidget(self.deny)
        row.addWidget(self.allow)
        layout.addLayout(row)
        self.allow.setAutoDefault(False)
        self.allow.setDefault(False)
        self.deny.setAutoDefault(True)
        self.deny.setDefault(True)
        self.allowed, self.dismissed = False, True
        self.allow.setEnabled(False)
        self.deny.setFocus()
        centre_on(self, parent)
        QTimer.singleShot(self.SETTLE_MS, lambda: self.allow.setEnabled(True))

    def _allowed(self):
        self.allowed, self.dismissed = True, False
        self.accept()

    def _denied(self):
        self.dismissed = False  # a pressed button, so Always remember counts
        self.reject()

    def reject(self):
        self.allowed = False
        super().reject()

    def answer(self):
        """(allow, remember). Escape or closing the dialog answers no and remembers nothing."""
        return self.allowed, bool(self.remember.isChecked() and not self.dismissed)


IMAGE_TIP = ('The AI may only install this exact start-up menu image (checked by its SHA-256 before anything is '
             'written). If the image changes, you are asked again.')


def image_line(image):
    return (f'Start-up menu it may install: {image}' if image else
            'No start-up menu image was found, so the AI cannot install or repair the menu.')


class AiKeepDialog(QDialog):
    """A remembered Allow is only a preference: once per run (and whenever the start-up menu
    image changes) the owner confirms it with a click on KEEP. TURN OFF is the default
    (Enter, Escape and closing choose it); KEEP ignores input for its first second."""
    SETTLE_MS = AskDialog.SETTLE_MS

    def __init__(self, parent, requested_by, action=None, image=None, changed=False):
        super().__init__(parent)
        self.setWindowTitle('Keep allowing the AI?')
        self.setModal(True)
        fit_dialog(self, parent, 520)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(12)
        head = ('The start-up menu image has changed.' if changed else
                'The AI is allowed to use Chromatic Manager.')
        layout.addWidget(label(head, 'info', size=theme.GEOMETRY['base_point_size'] + 4, bold=True))
        who = program_name(requested_by) or 'A program on this PC'
        asks = f' It asks to: {action.title}.' if action is not None else ''
        layout.addWidget(label(f'{who} wants to use it now.{asks} Keep allowing it this time?', 'value',
                               size=theme.GEOMETRY['base_point_size'] + 1))
        self.image = label(image_line(image), 'update', bold=True)
        self.image.setToolTip(IMAGE_TIP)
        layout.addWidget(self.image)
        row = QHBoxLayout()
        row.addStretch(1)
        self.off = button('Turn off', 'no')
        self.off.setToolTip('The AI needs your Approve for each change until this app closes.')
        self.off.clicked.connect(self.reject)
        self.keep = button('Keep', 'yes')
        self.keep.clicked.connect(self.accept)
        row.addWidget(self.off)
        row.addWidget(self.keep)
        layout.addLayout(row)
        self.keep.setAutoDefault(False)
        self.keep.setDefault(False)
        self.off.setAutoDefault(True)
        self.off.setDefault(True)
        self.keep.setEnabled(False)
        self.off.setFocus()
        centre_on(self, parent)
        QTimer.singleShot(self.SETTLE_MS, lambda: self.keep.setEnabled(True))


def approve_prompt(action, requested_by=None, reason='', release=None):
    """What the approval dialog says: what will be done (for an install, the version and
    image it writes), and for a program's request who asked and, clearly labelled as its
    words, why (at most REASON_MAX characters). Only the person at this window can approve."""
    asked = ''
    if requested_by:
        said = ' '.join(str(reason or '').split())
        said = said if len(said) <= REASON_MAX else said[:REASON_MAX - 3] + '...'
        asked = (f'Request from {requested_by}.\n'
                 + (f'The AI says: "{said}"\n' if said else '')
                 + 'Nothing happens unless you press Approve here; the program cannot approve it.\n\n')
    what = action.help if action.confirm else f'{action.help}\nIt changes nothing on the console.'
    image = f'\nStart-up menu to install: {release}' if release and action.key in ('install', 'update') else ''
    if action.key == 'fpga_media':  # the exact console chip image this press would write
        import chromatic_fpga
        image = f'\nConsole chip image to write: {chromatic_fpga.media_text() or "none built yet (refused)"}'
    if action.key == 'fpga_trial':  # the exact image this press would load into the chip's RAM (never the flash)
        import chromatic_fpga
        image = (f'\nConsole chip image to try from RAM: {chromatic_fpga.media_text() or "none built yet (refused)"}'
                 '\nThe flash is not written; switching the console off and on returns to the flash image.')
    if action.key == 'fpga_link_trial':  # the reviewed 18.41 image, into the chip's RAM only
        import chromatic_fpga
        image = (f'\nConsole chip image to try from RAM: {chromatic_fpga.link_ready_text() or "none ready (refused)"}'
                 '\nThe flash is not written; switching the console off and on returns to the flash image.')
    if action.key == 'fpga_link':  # the exact image this press would write, after the confirmed try of it
        import chromatic_fpga
        image = (f'\nConsole chip image to write: {chromatic_fpga.link_ready_text() or "none ready (refused)"}'
                 '\nOnly the image you tried and answered All good to is written.')
    if action.key == 'fpga_link_update':  # a program's request for the one step: this Approve only starts it
        import chromatic_fpga
        image = (f'\nConsole chip image to try from RAM and check: '
                 f'{chromatic_fpga.link_ready_text() or "none ready (refused)"}'
                 '\nThis Approve starts the try; it writes nothing. You are asked once more, with the new image '
                 'running, before it is written.')
    if action.key == 'fpga_restore':
        image = '\nWrites your original console chip image from the verified backup (both disk copies checked).'
    return (f'{asked}{action.title}?\n{what}{image}\n\nKeep your Chromatic plugged in and switched on until this '
            'app says Done.')


def keep_prompt(text):
    """What the one-step update's Approve dialog says under its big headline, while the new image
    runs from the console chip's RAM: the job's own words (start a game, look; Not right changes
    nothing) and the exact image Approve writes."""
    import chromatic_fpga
    return (f'{text}\n\nApprove writes this image to the console chip: '
            f'{chromatic_fpga.link_ready_text() or "the image tried just now"}. All 4 MiB are read back and compared.'
            '\n\nAfter Approve, keep your Chromatic plugged in and switched on until this app says Done.')


PILOT_ART = {'ok': 'ok', 'error': 'error', 'busy': 'busy', 'update': 'busy', 'info': 'info'}  # the console's face
PILOT_PAUSED_TIP = ('Your Chromatic has one USB cable. While this app installs or checks the console, Remote Pilot '
                    'lets go of it and keeps working over Wi-Fi, then gets it back when the job ends.')
SOUND_TIP = ('One button, five steps, about 15 minutes, with you at the console: Check FPGA (reads only), Restore '
             'FPGA over your original (the practice undo), Try FPGA image (from the chip\'s memory, nothing written), '
             'then FPGA sound. Each job still asks for your Approve, and you can stop on any screen.')
LINK_TIP = ('One button, five steps, about 15 minutes, with you at the console: Check FPGA (reads only, it must hold the '
            'sound image), Try Pocket link (from the chip\'s memory, nothing written), four quick checks on the console, '
            'then the Pocket link image written and checked. Each job asks for your Approve, and you can stop on any '
            'screen.')
LINK_UPDATE_TIP = ('One press, about 6 minutes, no question rounds: this app checks the console chip, runs the Pocket '
                   'link image from the chip\'s memory and checks it by itself. Then, with the new image running, you '
                   'start a game, look, and press Approve once; it is written, read back, and the start-up menu is '
                   'brought up to date (its own Approve, only when it is older). If a check fails or you press Not '
                   'right, nothing is written. Step by step (four checks you answer on the console) is offered on its '
                   'first screen.')
UPDATE_SOUND_TIP = ('Your console chip runs an older sound image. The same five guided steps as the setup: Check FPGA, '
                    'your original image written back over the older one and checked (you approve writing over it), '
                    'the new image tried from the chip\'s memory, then written and checked. Each job asks for your '
                    'Approve, and you can stop on any screen.')


def sound_line(sound, reported, built_text=None, link=None):
    """(text, colour role, tooltip) for the Home tab's small sound line, or None for no line.
    `sound` is chromatic_manager_core.sound_state(); 'n/a' when Home is not on Up to date.
    `link`: chromatic_manager_core.link_state() ('ready' on the sound + Pocket link image)."""
    version = (reported or '').replace(' debug', ' with the debug bit')
    target = built_text or f'sound build {SOUND_FPGA[0]}.{SOUND_FPGA[1]}'
    if sound == 'installed' and link == 'ready':
        return ('Sound: on  |  Pocket link: ready', 'ok',
                'The start-up menu reports that the console chip (FPGA) runs the sound + Pocket link image'
                f'{" (version " + version + ")" if version[:1].isdigit() else ""}, so Jellyfin plays with sound and its '
                'menu has Pocket link. FPGA sound on Fix a problem puts the sound image back; Restore FPGA your '
                'original image.')
    if sound == 'installed':
        return ('Sound: on', 'ok',
                f'The start-up menu reports that the console chip (FPGA) runs the sound image'
                f'{" (version " + version + ")" if version[:1].isdigit() else ""}, so Jellyfin plays with sound. '
                'Nothing to set up. Check FPGA on Fix a problem compares the chip with the image byte for byte; '
                'Restore FPGA puts your original image back.')
    if sound == 'outdated':
        return ('Sound: older image', 'update',
                f'The start-up menu reports an older sound image on the console chip (version {version}). This app '
                f'has {target}. Update sound puts the new one on, in the same guided steps as the setup.')
    if sound == 'none':
        return ('Sound: not set up', 'dim',
                'The console chip runs ModRetro\'s original image (18.14), so Jellyfin plays without sound.'
                + (' Set up Jellyfin sound adds it.' if built_text else ' No sound image is built on this PC yet.'))
    if sound == 'unknown':
        return ('Unknown chip image. See Check FPGA.', 'warn',
                f'The console chip reports {("version " + version) if version[:1].isdigit() else "no version"}: '
                f'neither ModRetro\'s 18.14 nor this app\'s sound image {SOUND_FPGA[0]}.{SOUND_FPGA[1]} or Pocket '
                f'link image {LINK_FPGA[0]}.{LINK_FPGA[1]} (for example after an official ModRetro update). Nothing is set up from here. Check FPGA on Fix a '
                'problem reads the chip and says what it holds.')
    if sound is None:
        return ('Sound: not checked', 'dim',
                'This start-up menu does not report the console chip (FPGA) version; newer ones do. Check FPGA on '
                'Fix a problem says whether the sound image is there. Set up Jellyfin sound is there too.')
    return None

VERSION_CHANGE_PROMPT = 'Type change to continue'  # the manager's question before a version-changing restore
WIFI_LIST_PROMPT = 'Wi-Fi networks the console will have'  # chromatic_manager.WIFI_LIST_PROMPT (the tests check they match)


def fpga_change_prompt():
    """Restore FPGA found neither the original nor the sound image on the console chip."""
    return ('The console chip (FPGA) holds neither your original image nor the sound image: for example a newer '
            'official ModRetro firmware, or an interrupted write.\n\nApprove writes your original console chip image '
            'from your backup over it, reads it all back and compares, then restarts the console. If an official '
            'update changed the console, Cancel and use Check FPGA and the runbook first.\n\n'
            'Keep your Chromatic plugged in and switched on until this app says Done.')


def version_change_prompt(lines):
    """The second approval of a restore that changes the stock firmware version."""
    said = next((line.strip() for line in reversed(lines) if 'This changes the stock firmware from' in line), '')
    return ('The original firmware version will change.\n'
            + (f'{said}\n' if said else '')
            + '\nYour console is backed up as it is now first. ModRetro\'s settings are kept; the start-up menu\'s '
              'Wi-Fi networks (also ones added on the console), PC pairing and Jellyfin sign-in are erased.\n'
              'Approve to restore that version. Cancel (or Esc) changes nothing.')


def modretro_updater_running():
    """Is ModRetro's firmware updater (ChromaticFirmwareUpdater*.exe) running? It reboots the
    console while it works, so the window must not take the port then. Process names only;
    if the list cannot be read, it counts as running (the person then presses the button)."""
    if os.name != 'nt':
        return False
    import subprocess
    try:
        listed = subprocess.run(['tasklist', '/FO', 'CSV', '/NH'], capture_output=True, text=True, timeout=10,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).stdout
    except Exception:
        return True
    names = (line.split(',')[0].strip().strip('"').lower() for line in listed.splitlines() if line.strip())
    return any(name.startswith('chromaticfirmwareupdater') for name in names)


def physical_step(phase, mode, asking):
    """What the person physically does while a job runs, in one plain instruction."""
    if asking:
        return 'Answer the question in the window.'
    if phase == 'Waiting for the console':
        return 'Plug in your Chromatic and switch it on.'
    if mode == 'download':
        return 'Wait. Don\'t switch off or unplug your Chromatic. Its screen stays frozen until this finishes.'
    if mode == 'restarting':
        return 'Wait. Your Chromatic is restarting by itself. Don\'t press its buttons until this app says Done.'
    return 'Wait. Keep your Chromatic plugged in and switched on.'


class Sign(QWidget):
    """A small pixel sign, our own: a badge in the meaning's colour with a dark glyph. A tick
    (green: fine), "!" (yellow: attention or working), "x" (red: a problem, with what to do next
    beside it), "i" (blue: plain information). Drawn cell by cell like the tab icons."""

    def __init__(self, tone='info', cell=2):
        super().__init__()
        self.cell, self.tone = cell, tone
        self.setFixedSize(9 * cell, 9 * cell)

    def set_tone(self, tone):
        self.tone = tone
        self.setAccessibleName({'ok': 'fine', 'warn': 'attention', 'error': 'problem'}.get(self.meaning(), 'information'))
        self.update()

    def meaning(self):
        return theme.SIGN_TONES.get(self.tone, 'info')

    def paintEvent(self, event):
        p = QPainter(self)
        c, face = self.cell, colour(self.meaning())
        p.fillRect(c, 0, 7 * c, 9 * c, face)  # cut corners, like the pixel buttons
        p.fillRect(0, c, 9 * c, 7 * c, face)
        theme.draw_icon(p, f'sign_{self.meaning()}', 2 * c, 2 * c, c, theme.PALETTE['window'])
        p.end()


OWNER_TONES = {owner_actions.WAITING: 'update', owner_actions.DONE: 'ok', owner_actions.TIMEOUT: 'error'}
OWNER_TAGS = {owner_actions.WAITING: 'YOUR TURN: DO THIS NOW', owner_actions.DONE: 'ALL GOOD',
              owner_actions.TIMEOUT: 'THIS NEEDS YOU'}


class DrainBar(QWidget):
    """The time left for an owner action: a row of pixel blocks that empties from the right."""
    BLOCKS = 24

    def __init__(self):
        super().__init__()
        self.share, self.ink = 1.0, theme.PALETTE['warn']
        self.setFixedHeight(12)
        self.setMinimumWidth(240)

    def set_share(self, share, ink=None):
        self.share = max(0.0, min(1.0, float(share)))
        self.ink = ink or self.ink
        self.update()

    def lit(self):
        return int(math.ceil(self.share * self.BLOCKS - 1e-9))

    def paintEvent(self, event):
        p, gap = QPainter(self), 2
        w = max(2, (self.width() - gap * (self.BLOCKS - 1)) // self.BLOCKS)
        x0 = (self.width() - (w * self.BLOCKS + gap * (self.BLOCKS - 1))) // 2
        for i in range(self.BLOCKS):
            on = i < self.lit()
            p.fillRect(x0 + i * (w + gap), 0, w, self.height(), colour(self.ink if on else 'progress_off_lo'))
            p.fillRect(x0 + i * (w + gap), 0, w, 2, colour(self.ink if on else 'progress_off_mid'))
        p.end()


class OwnerBanner(QWidget):
    """What the running job needs the owner to do NOW, filling the speech bubble (owner rule,
   : a request that was only in the log was missed and the job timed out). Yellow with a
    blinking border and a live countdown while it waits; green "Done, thank you" when the job saw
    it happen; red "Time ran out" with the plain next step and OK when it did not. The wording comes
    from the job (chromatic_manager_owner); this only draws it, in the app's pixel style."""

    def __init__(self):
        super().__init__()
        self.state, self.lit, self.left = None, True, None
        base, small = theme.GEOMETRY['base_point_size'], theme.GEOMETRY['small_point_size']
        ink = theme.PALETTE['warn']
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(6)
        top = QHBoxLayout()
        top.setSpacing(8)
        top.addStretch(1)
        self.sign = Sign('update', 3)
        top.addWidget(self.sign, 0, Qt.AlignmentFlag.AlignVCenter)
        self.tag = PixelHead('', ink, 2)
        top.addWidget(self.tag, 0, Qt.AlignmentFlag.AlignVCenter)
        top.addStretch(1)
        layout.addLayout(top)
        layout.addStretch(1)
        self.head = PixelHead('', ink, 3, wrap=True, centre=True)
        layout.addWidget(self.head)
        self.text = label('', 'value', size=base + 1, bold=True)
        self.text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.text)
        self.asked = label('', 'dim', size=small)
        self.asked.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.asked)
        count = QHBoxLayout()
        count.setSpacing(10)
        count.addStretch(1)
        self.count = PixelText('', ink, 6)
        count.addWidget(self.count, 0, Qt.AlignmentFlag.AlignVCenter)
        self.count_unit = label('seconds left', 'notice', wrap=False, size=base + 1, bold=True)
        count.addWidget(self.count_unit, 0, Qt.AlignmentFlag.AlignVCenter)
        count.addStretch(1)
        layout.addLayout(count)
        self.drain = DrainBar()
        layout.addWidget(self.drain)
        layout.addStretch(1)
        row = QHBoxLayout()
        row.addStretch(1)
        self.stop = button('Stop the AI', 'stop')
        self.stop.setFixedHeight(32)
        self.stop.hide()
        row.addWidget(self.stop, 2)
        self.ok = button('OK', 'go', size=base + 2)
        self.ok.setFixedHeight(36)
        self.ok.setMinimumWidth(150)
        self.ok.setToolTip('Closes this sign and shows how the job ended.')
        self.ok.hide()
        row.addWidget(self.ok, 2)
        row.addStretch(1)
        layout.addLayout(row)

    def tone(self):
        return OWNER_TONES.get(self.state, 'update')

    def show_action(self, action):
        """Draw one owner action (the dictionary chromatic_manager_owner announces)."""
        self.state = state = action['state']
        waiting, timed_out = state == owner_actions.WAITING, state == owner_actions.TIMEOUT
        ink = TONE_INK[self.tone()]
        self.sign.set_tone(self.tone())
        self.tag.set_colours(ink)
        self.tag.setText(OWNER_TAGS.get(state, ''))
        self.head.set_colours(ink)
        self.head.setText(action['title'] if waiting else
                          owner_actions.TIMED_OUT if timed_out else owner_actions.THANKS)
        self.text.setText(action['instruction'] if waiting else
                          f'What to do now: {action["next"]}' if timed_out else action['note'])
        self.text.setVisible(bool(self.text.raw_text()))
        self.asked.setText('' if waiting else f'You were asked: {action["title"]}')
        self.asked.setVisible(not waiting)
        timed = waiting and action['seconds'] is not None
        for widget in (self.count, self.count_unit, self.drain):
            widget.setVisible(timed)
        self.ok.setVisible(timed_out)
        self.lit = True
        self.setAccessibleName(f'{self.head.raw_text()}. {self.text.raw_text()}')
        self.update()

    def set_left(self, left, allowed):
        """The seconds left (None: this action has no time limit)."""
        self.left = left
        if left is None or not allowed:
            return
        self.count.setText(str(int(left)))
        self.drain.set_share(left / allowed, theme.PALETTE['error' if left <= 10 else 'warn'])
        self.count.set_colours(theme.PALETTE['error' if left <= 10 else 'warn'])

    def tick(self):
        """The border blinks while it waits for the owner (twice a second)."""
        self.lit = not self.lit if self.state == owner_actions.WAITING else True
        self.update()

    def paintEvent(self, event):
        p, r = QPainter(self), self.rect()
        ink = QColor(TONE_INK[self.tone()])
        edge = ink if self.lit else ink.darker(300)
        t = 4
        for x, y, w, h in ((t, 0, r.width() - 2 * t, t), (t, r.height() - t, r.width() - 2 * t, t),
                           (0, t, t, r.height() - 2 * t), (r.width() - t, t, t, r.height() - 2 * t)):
            p.fillRect(x, y, w, h, edge)  # a thick frame with cut corners, like the pixel buttons
        p.end()


def toggle_privacy(widget):
    """The eye inside a dialog: the window's own toggle when there is one (so its eye matches)."""
    window = widget.parent()
    while window is not None and not hasattr(window, 'set_privacy'):
        window = window.parent()
    if window is not None:
        window.set_privacy(not Privacy.on)
    else:
        Privacy.set(not Privacy.on)


def privacy_button(on_toggle):
    eye = button('', 'privacy')
    eye.setCheckable(True)
    eye.setChecked(Privacy.on)
    eye.setFixedSize(34, 28)
    eye.setToolTip(PRIVACY_TIP_ON if Privacy.on else PRIVACY_TIP_OFF)
    eye.clicked.connect(on_toggle)
    return eye


def shown_name(name):
    """A whole private name (a Wi-Fi network, a server's name) as the window shows it now."""
    return mask_name(name) if Privacy.on else str(name)


# ---- the console's Wi-Fi list ---------------------------------------
_NETWORK_LINE = re.compile(r'^\s*(\d+)\. network "(.*)" \(([^()]*)\)\s*$')


def parse_network_prompt(prompt):
    """([(name, note)], problem) from the manager's Wi-Fi list question (network_list_prompt)."""
    entries, problem = [], ''
    for line in str(prompt).split('\n')[1:]:
        found = _NETWORK_LINE.match(line)
        if found:
            entries.append((found.group(2), found.group(3)))
        elif line.strip() and not line.startswith('Type ok'):
            problem = line.strip()
    return entries, problem


class WifiListDialog(QDialog):
    """The Wi-Fi list the console will have, before anything is written: networks already on
    the console (kept, passwords as they are) and the one(s) from this setup. Up moves a
    network toward the top (the console tries them from the top), Remove takes one off (Put
    back undoes it). At most 4: Write stays off, with a red sign and the plain reason, until it
    fits. Names show as **** while private details are hidden (the eye here or on the window).
    Like the other dialogs, Write ignores input for its first second; Cancel writes nothing.
    `answer`: 'ok', the new order such as '2,1' (numbers left out are removed), or '' (cancel)."""
    SETTLE_MS = AskDialog.SETTLE_MS
    MAX = 4
    NOTE_TONES = {'new': 'ok', 'new password': 'update'}

    def __init__(self, parent, prompt):
        super().__init__(parent)
        self.setWindowTitle('Wi-Fi on your Chromatic')
        self.setModal(True)
        fit_dialog(self, parent, 580)
        self.entries, self.problem = parse_network_prompt(prompt)
        self.order = list(range(len(self.entries)))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(10)
        top = QHBoxLayout()
        top.addWidget(label('WI-FI ON YOUR CHROMATIC', 'info', wrap=False, size=theme.GEOMETRY['base_point_size'] + 3,
                            bold=True))
        top.addStretch(1)
        self.eye = privacy_button(self._toggle_privacy)
        top.addWidget(self.eye)
        layout.addLayout(top)
        layout.addWidget(label('Your Chromatic tries these from the top. Networks already saved on it keep their '
                               'passwords. Up moves one higher; Remove takes off one you no longer use.', 'dim',
                               size=theme.GEOMETRY['small_point_size']))
        self.rows = QVBoxLayout()
        self.rows.setSpacing(5)
        layout.addLayout(self.rows)
        status = QHBoxLayout()
        self.sign = Sign()
        status.addWidget(self.sign, 0, Qt.AlignmentFlag.AlignVCenter)
        self.status = label('', 'ok', size=theme.GEOMETRY['small_point_size'] + 0.5, bold=True)
        status.addWidget(self.status, 1)
        layout.addLayout(status)
        row = QHBoxLayout()
        row.addStretch(1)
        self.cancel = button('Cancel', 'no')
        self.cancel.setToolTip('Writes nothing. The console keeps its networks as they are.')
        self.cancel.clicked.connect(self.reject)
        self.write = button('Write this list', 'yes')
        self.write.setToolTip('Writes this list to the console (both copies, checked on the chip).')
        self.write.clicked.connect(self.accept)
        row.addWidget(self.cancel)
        row.addWidget(self.write)
        layout.addLayout(row)
        for widget in (self.cancel, self.write):
            widget.setAutoDefault(False)
            widget.setDefault(False)
        self.cancel.setAutoDefault(True)
        self.cancel.setDefault(True)
        self._settled = False
        self._rebuild()
        self.write.setEnabled(False)
        centre_on(self, parent)
        QTimer.singleShot(self.SETTLE_MS, self._settle)

    def _settle(self):
        self._settled = True
        self._refresh_status()

    def _toggle_privacy(self):
        toggle_privacy(self)
        self.eye.setChecked(Privacy.on)
        self.eye.setToolTip(PRIVACY_TIP_ON if Privacy.on else PRIVACY_TIP_OFF)
        self._rebuild()

    def removed(self):
        return [n for n in range(len(self.entries)) if n not in self.order]

    def move_up(self, index):
        at = self.order.index(index)
        if at > 0:
            self.order[at - 1], self.order[at] = self.order[at], self.order[at - 1]
            self._rebuild()

    def remove(self, index):
        if index in self.order:
            self.order.remove(index)
            self._rebuild()

    def put_back(self, index):
        if index not in self.order:
            self.order.append(index)
            self._rebuild()

    def _row(self, number, index, removed=False):
        name, note = self.entries[index]
        widget = QWidget()
        line = QHBoxLayout(widget)
        line.setContentsMargins(0, 0, 0, 0)
        line.setSpacing(6)
        line.addWidget(label('--' if removed else f'{number}.', 'dim' if removed else 'value', wrap=False,
                             size=theme.GEOMETRY['small_point_size'], bold=True))
        sign = Sign('error' if removed else self.NOTE_TONES.get(note, 'info'))
        line.addWidget(sign, 0, Qt.AlignmentFlag.AlignVCenter)
        title = label(shown_name(name), 'dim' if removed else 'value', wrap=False,
                      size=theme.GEOMETRY['small_point_size'] + 0.5, bold=not removed)
        title.setToolTip('Hidden while private details are hidden (the eye).' if Privacy.on else '')
        line.addWidget(title, 2)
        line.addWidget(label('removed when you write' if removed else note, 'error' if removed else 'dim', wrap=False,
                             size=theme.GEOMETRY['small_point_size'] - 0.5), 2)
        if removed:
            back = button('Put back', '', size=theme.GEOMETRY['small_point_size'])
            back.setFixedSize(146, 26)
            back.setToolTip('Keeps this network on the console after all.')
            back.clicked.connect(lambda _=False, i=index: self.put_back(i))
            line.addWidget(back, 1)
        else:
            up = button('Up', '', size=theme.GEOMETRY['small_point_size'])
            up.setFixedSize(56, 26)
            up.setEnabled(number > 1)
            up.setToolTip('Moves this network one higher: the console tries it sooner.')
            up.clicked.connect(lambda _=False, i=index: self.move_up(i))
            gone = button('Remove', 'stop', size=theme.GEOMETRY['small_point_size'])
            gone.setFixedSize(84, 26)
            gone.setToolTip('Takes this network off the console when you write the list (Put back undoes it).')
            gone.clicked.connect(lambda _=False, i=index: self.remove(i))
            line.addWidget(up, 1)
            line.addWidget(gone, 1)
        return widget

    def _rebuild(self):
        while self.rows.count():
            item = self.rows.takeAt(0)
            if item.widget() is not None:
                item.widget().hide()
                item.widget().deleteLater()
        for number, index in enumerate(self.order, 1):
            self.rows.addWidget(self._row(number, index))
        for index in self.removed():
            self.rows.addWidget(self._row(0, index, removed=True))
        self._refresh_status()

    def problem_now(self):
        """None when the list can be written, else the plain reason (red)."""
        count = len(self.order)
        if count == 0:
            return 'Keep at least one network.'
        if count > self.MAX:
            extra = count - self.MAX
            return f'Your Chromatic keeps at most {self.MAX} networks. Remove {extra} to go on.'
        return None

    def _refresh_status(self):
        problem = self.problem_now()
        if problem:
            self.sign.set_tone('error')
            self.status.setProperty('role', 'error')
            self.status.setText(problem)
        else:
            count, gone = len(self.order), len(self.removed())
            self.sign.set_tone('ok')
            self.status.setProperty('role', 'ok')
            self.status.setText(f'{count} network{"s" if count != 1 else ""} on the console'
                                + (f', {gone} removed' if gone else '') + '. Ready to write.')
        restyle(self.status)
        self.write.setEnabled(self._settled and problem is None)

    def answer(self):
        if self.result() != QDialog.DialogCode.Accepted or self.problem_now():
            return ''
        if self.order == list(range(len(self.entries))):
            return 'ok'
        return ','.join(str(n + 1) for n in self.order)


# ---- Jellyfin on this PC -----------------------------------------------
def normalise_server(text):
    """A server address as typed: http:// and the Jellyfin port 8096 added when missing; no
    credentials, query or fragment (connection_config.validate). ValueError: a plain sentence."""
    from urllib.parse import urlsplit
    import connection_config as cc
    text = (text or '').strip().rstrip('/')
    if not text:
        raise ValueError('Type the server address, or press Find servers.')
    if '://' not in text:
        text = 'http://' + text
        try:
            parts = urlsplit(text)
            if parts.port is None and parts.hostname:
                text = f'http://{parts.hostname}:8096{parts.path}'
        except ValueError:
            pass
    try:
        return cc.validate({'server_url': text, 'access_token': 'x'}).server_url
    except ValueError:
        raise ValueError('That is not a server address. Type it like 192.0.2.20:8096.') from None


def default_finder():
    import jellyfin_discovery
    return jellyfin_discovery.discover()


def default_tester(connection):
    import chromatic_jellyfin_link as jl
    return jl.check_pc_connection(connection)


def default_saver(connection, path):
    import connection_config as cc
    return cc.save_connection(connection, path)


def default_loader(path):
    import connection_config as cc
    return cc.load_connection(Path(path)) if Path(path).exists() else None


class _Done(QObject):
    done = Signal(object)


class JellyfinDialog(QDialog):
    """Settings > Jellyfin server: the server this PC uses and its sign-in, which Chromatic
    Manager uses to give the console its own Jellyfin sign-in (Quick Connect). Server address
    with Find servers (Jellyfin's LAN discovery, as the Pocket installer does), user name and a
    hidden password; Test shows yes/no only (server found, sign-in works, Quick Connect on);
    Save writes the PC's private file through connection_config (local code; never printed,
    logged or put in arguments). The password field starts empty: leave it empty to keep the
    saved one. After a save, New Jellyfin sign-in starts that job (with its own Approve).
    `choice`: 'relink' when the owner pressed New Jellyfin sign-in, else None."""

    def __init__(self, parent, path=None, tester=None, finder=None, saver=None, loader=None):
        super().__init__(parent)
        import connection_config as cc
        self.path = Path(path or cc.DEFAULT_CONFIG)
        self.tester, self.finder = tester or default_tester, finder or default_finder
        self.saver, self.loader = saver or default_saver, loader or default_loader
        self.choice, self.busy, self.saved_now, self.last = None, False, False, None  # last: (connection, result)
        self._save_anyway = None
        try:
            self.saved = self.loader(self.path)
        except (OSError, ValueError):
            self.saved = None
        self._bridge = _Done(self)
        self._bridge.done.connect(self._finished)
        self.setWindowTitle('Jellyfin server')
        self.setModal(True)
        fit_dialog(self, parent, 600)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 18, 22, 16)
        layout.setSpacing(8)
        top = QHBoxLayout()
        top.addWidget(label('JELLYFIN ON THIS PC', 'info', wrap=False, size=theme.GEOMETRY['base_point_size'] + 3,
                            bold=True))
        top.addStretch(1)
        self.eye = privacy_button(self._toggle_privacy)
        top.addWidget(self.eye)
        layout.addLayout(top)
        layout.addWidget(label('Chromatic Manager uses this to give your Chromatic its own Jellyfin sign-in. Your '
                               'password stays on this PC, in its private settings file.', 'dim',
                               size=theme.GEOMETRY['small_point_size']))
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)
        small = theme.GEOMETRY['small_point_size']
        self.server = QLineEdit()
        self.server.setPlaceholderText('192.0.2.20:8096')
        self.server.setToolTip('Your Jellyfin server\'s address on your home network. Find servers looks for it.')
        self.find = button('Find servers', '', size=small)
        self.find.setFixedHeight(28)
        self.find.setToolTip('Asks your home network which Jellyfin servers are there (about 2 seconds).')
        self.find.clicked.connect(self.find_servers)
        self.user = QLineEdit()
        self.user.setToolTip('Your Jellyfin user name.')
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setToolTip('What you type is hidden and never shown, logged or sent anywhere but your server.')
        token_only = bool(self.saved and self.saved.access_token)
        if self.saved:
            self.server.setText(self.saved.server_url)
            self.user.setText(self.saved.username)
        if token_only:
            self.user.setPlaceholderText('signed in with a saved token')
            self.password.setPlaceholderText('saved token kept while both are empty')
        elif self.saved and self.saved.password:
            self.password.setPlaceholderText('saved: leave empty to keep it')
        for n, (name, widget) in enumerate((('Server', self.server), ('User name', self.user),
                                            ('Password', self.password))):
            widget.setFont(theme.body_font(theme.GEOMETRY['base_point_size']))
            grid.addWidget(label(name, 'dim', wrap=False, size=small), n, 0)
            grid.addWidget(widget, n, 1)
        grid.addWidget(self.find, 0, 2)
        grid.setColumnStretch(1, 1)
        layout.addLayout(grid)
        self.found_row = QHBoxLayout()
        self.found_row.setSpacing(6)
        layout.addLayout(self.found_row)
        self.results = {}
        for key, name in (('reachable', 'Server found'), ('signed_in', 'Sign-in works'),
                          ('quick_connect', 'Quick Connect on (the console signs in with it)')):
            line = QHBoxLayout()
            sign = Sign('info')
            text = label(f'{name}: not tested yet', 'dim', wrap=False, size=small)
            line.addWidget(sign, 0, Qt.AlignmentFlag.AlignVCenter)
            line.addWidget(text, 1)
            layout.addLayout(line)
            self.results[key] = (sign, text, name)
        self.note = label('', 'notice', size=small + 0.5, bold=True)
        self.note.hide()
        layout.addWidget(self.note)
        row = QHBoxLayout()
        row.addStretch(1)
        self.close_button = button('Close', 'no')
        self.close_button.setToolTip('Closes this page. Nothing more is saved.')
        self.close_button.clicked.connect(self.reject)
        self.test = button('Test', '')
        self.test.setToolTip('Checks the address and the sign-in with your server. Shows yes or no only; saves nothing.')
        self.test.clicked.connect(self.test_now)
        self.save = button('Save', 'yes')
        self.save.setToolTip('Tests, then saves these settings in this PC\'s private file.')
        self.save.clicked.connect(self.save_now)
        self.relink = button('New Jellyfin sign-in', 'go')
        self.relink.setToolTip('Gives your Chromatic its own new Jellyfin sign-in by Quick Connect now (asks for your '
                               'Approve, then about 1 minute with the console plugged in).')
        self.relink.clicked.connect(self._relink)
        self.relink.hide()
        for widget in (self.close_button, self.test, self.save, self.relink):
            widget.setFixedHeight(34)
            widget.setAutoDefault(False)
            widget.setDefault(False)
            row.addWidget(widget)
        layout.addLayout(row)
        for field in (self.server, self.user, self.password):
            field.textEdited.connect(self._edited)
        self._apply_privacy()
        centre_on(self, parent)

    # privacy: the address and user name show only while private details are showing (or while typed)
    def _apply_privacy(self):
        mode = QLineEdit.EchoMode.PasswordEchoOnEdit if Privacy.on else QLineEdit.EchoMode.Normal
        self.server.setEchoMode(mode)
        self.user.setEchoMode(mode)
        for index in range(self.found_row.count()):
            widget = self.found_row.itemAt(index).widget()
            if isinstance(widget, FitButton) and hasattr(widget, 'server_name'):
                widget.setText(shown_name(widget.server_name))

    def _toggle_privacy(self):
        toggle_privacy(self)
        self.eye.setChecked(Privacy.on)
        self.eye.setToolTip(PRIVACY_TIP_ON if Privacy.on else PRIVACY_TIP_OFF)
        self._apply_privacy()

    def _edited(self, *_):
        self._save_anyway = None
        if self.saved_now:
            self.saved_now = False
            self.relink.hide()
        self.save.setText('Save')
        self.save.setProperty('kind', 'yes')

    def _say(self, tone, text):
        self.note.setProperty('role', {'ok': 'ok', 'error': 'error'}.get(tone, 'notice'))
        self.note.setText(text)
        self.note.setVisible(bool(text))
        restyle(self.note)

    def _set_busy(self, busy, what=''):
        self.busy = busy
        for widget in (self.find, self.test, self.save, self.relink, self.server, self.user, self.password):
            widget.setEnabled(not busy)
        if busy:
            self._say('info', what)

    def _run(self, then, work):
        """Network work off the window's thread; `then(result)` back on it."""
        self._set_busy(True, 'Working... (a few seconds)')

        def body():
            try:
                out = work()
            except Exception as error:  # a generic reason only; never the error's text
                out = error
            self._bridge.done.emit((then, out))
        threading.Thread(target=body, name='chromatic-jellyfin-page', daemon=True).start()

    def _finished(self, payload):
        then, out = payload
        self._set_busy(False)
        then(out)

    def connection(self):
        """The Connection the fields describe (the saved password or token when left empty)."""
        import connection_config as cc
        server = normalise_server(self.server.text())
        user, password = self.user.text().strip(), self.password.text()
        saved = self.saved
        if not user and not password and saved and saved.access_token:
            return cc.validate({'server_url': server, 'access_token': saved.access_token})
        if not user:
            raise ValueError('Type your Jellyfin user name.')
        if not password and saved and not saved.access_token and user == saved.username:
            password = saved.password
        if any(ch in password for ch in '\r\n\0'):
            raise ValueError('The password cannot contain a line break.')
        return cc.validate({'server_url': server, 'username': user, 'password': password})

    def find_servers(self):
        while self.found_row.count():
            item = self.found_row.takeAt(0)
            if item.widget() is not None:
                item.widget().hide()
                item.widget().deleteLater()
        self._run(self._found, self.finder)

    def _found(self, servers):
        if isinstance(servers, Exception) or not servers:
            self._say('update', 'No server answered. Is it on? Type its address instead, for example '
                                '192.0.2.20:8096.')
            return
        self.found_row.addWidget(label('Found:', 'dim', wrap=False, size=theme.GEOMETRY['small_point_size']))
        for server in list(servers)[:3]:
            pick = button(shown_name(server.get('name') or 'Jellyfin'), 'go', size=theme.GEOMETRY['small_point_size'])
            pick.server_name = server.get('name') or 'Jellyfin'
            pick.setFixedHeight(26)
            pick.setToolTip('Uses this server. (Its address shows in the Server box.)')
            pick.clicked.connect(lambda _=False, a=server.get('address', ''): self._use(a))
            self.found_row.addWidget(pick)
        self.found_row.addStretch(1)
        self._say('ok', f'Found {len(servers)} server{"s" if len(servers) != 1 else ""}. Press one to use it.')

    def _use(self, address):
        self.server.setText(address)
        self._edited()
        self._say('info', 'Now type your user name and password, then press Save.')

    def _show_result(self, result):
        """The three yes/no lines and the one thing to do next."""
        for key, (sign, text, name) in self.results.items():
            value = bool(result.get(key)) if result else False
            sign.set_tone('ok' if value else 'error' if key != 'quick_connect' else 'update')
            text.setText(f'{name}: {"yes" if value else "no"}')
            text.setProperty('role', 'ok' if value else 'error' if key != 'quick_connect' else 'update')
            restyle(text)
        if not result or not result.get('reachable'):
            return 'error', ('The server did not answer. Is it on, and is the address right? Find servers looks on '
                             'your home network.')
        if not result.get('signed_in'):
            return 'error', 'The server is there, but the sign-in did not work. Check the user name and password.'
        if not result.get('quick_connect'):
            return 'update', ('Turn on Quick Connect in Jellyfin (Dashboard > General): the console signs in with '
                              'it.')
        return 'ok', 'Everything works.'

    def test_now(self):
        try:
            connection = self.connection()
        except ValueError as error:
            self._say('error', str(error))
            return
        self._run(lambda out: self._tested(connection, out), lambda: self.tester(connection))

    def _tested(self, connection, out, then=None):
        result = None if isinstance(out, Exception) else out
        self.last = (connection, result)
        tone, words = self._show_result(result)
        self._say(tone, words)
        if then:
            then(result)

    def save_now(self):
        try:
            connection = self.connection()
        except ValueError as error:
            self._say('error', str(error))
            return
        if self._save_anyway == connection:
            self._write(connection, None)
            return
        if self.last and self.last[0] == connection and self.last[1] is not None:
            self._after_test(connection, self.last[1])
            return
        self._run(lambda out: self._tested(connection, out, lambda result: self._after_test(connection, result)),
                  lambda: self.tester(connection))

    def _after_test(self, connection, result):
        if result and result.get('reachable') and result.get('signed_in'):
            self._write(connection, result)
            return
        self._save_anyway = connection
        self.save.setText('Save anyway')
        self.save.setProperty('kind', 'update')  # yellow: it saves settings that did not test well
        self.save.update()
        tone, words = self._show_result(result)
        self._say('error', f'Not saved. {words} Or press Save anyway to keep these settings.')

    def _write(self, connection, result):
        try:
            self.saved = self.saver(connection, self.path)
        except (OSError, ValueError):
            self._say('error', 'The settings could not be saved on this PC. Nothing was changed.')
            return
        self.saved_now, self._save_anyway = True, None
        self.password.clear()
        self.password.setPlaceholderText('saved: leave empty to keep it')
        self.save.setText('Save')
        self.save.setProperty('kind', 'yes')
        window = self.parent()
        if hasattr(window, 'add_line'):  # yes/no only, never a value
            window.add_line('Jellyfin settings saved on this PC (private file).'
                            + ('' if result is None else f' Server found: {"yes" if result.get("reachable") else "no"}; '
                               f'sign-in works: {"yes" if result.get("signed_in") else "no"}.'))
        self.relink.show()
        self._say('ok', 'Saved on this PC. Next: give your Chromatic a new Jellyfin sign-in.')

    def _relink(self):
        self.choice = 'relink'
        self.accept()

    def reject(self):
        if self.busy:
            return  # a test or search is running: it ends by itself within seconds
        super().reject()


class Dialogs:
    """Real dialogs. Tests replace this with scripted answers."""

    def __init__(self, parent):
        self.parent = parent
        self.lines = lambda: []  # the window sets this to its job's lines (for the version-change dialog)
        self.release = lambda: None  # and this to the start-up menu image an install would write

    def approve(self, text, approve_text='Approve', headline=None, cancel_text='Cancel'):
        dialog = ApproveDialog(self.parent, text, approve_text, headline, cancel_text)
        return dialog.exec() == QDialog.DialogCode.Accepted and dialog.approved

    def confirm(self, action, requested_by=None, reason=''):
        """The person's approval of one action. `requested_by` is set when a program asked
        through the local API: it is shown on the dialog, and still only a click approves."""
        return self.approve(approve_prompt(action, requested_by, reason, self.release()))

    def ai_control(self, requested_by, action=None, image=None):
        """The AI-control pop-up: (allow, remember)."""
        dialog = AiControlDialog(self.parent, requested_by, action, image)
        dialog.exec()
        return dialog.answer()

    def ai_keep(self, requested_by, action=None, image=None, changed=False):
        """"Keep allowing it this time?": True only after a click on Keep."""
        dialog = AiKeepDialog(self.parent, requested_by, action, image, changed)
        return dialog.exec() == QDialog.DialogCode.Accepted

    def ask(self, prompt, secret):
        """A question from a running job. The version-changing restore's typed `change` is an
        Approve dialog here, answered `change` only when approved. The one-step update's Approve,
        asked with the new image running, is one too: big headline, Approve or Not right, and only
        a click on Approve answers KEEP_ANSWER. Closing any dialog gives an empty answer, which
        the manager treats as no."""
        if not secret and str(prompt).startswith(VERSION_CHANGE_PROMPT):
            return 'change' if self.approve(version_change_prompt(self.lines()), 'Approve version change') else ''
        import chromatic_fpga
        if not secret and str(prompt).startswith(chromatic_fpga.LINK_KEEP_PROMPT):
            said = str(prompt)[len(chromatic_fpga.LINK_KEEP_PROMPT):]
            kept = self.approve(keep_prompt(said), 'Approve', chromatic_fpga.KEEP_TITLE, 'Not right')
            return chromatic_fpga.KEEP_ANSWER if kept else ''
        if not secret and str(prompt).startswith(chromatic_fpga.FPGA_UNKNOWN_PROMPT):
            return 'change' if self.approve(fpga_change_prompt(), 'Approve chip write') else ''
        if not secret and str(prompt).startswith(WIFI_LIST_PROMPT):
            dialog = WifiListDialog(self.parent, prompt)
            dialog.exec()
            return dialog.answer()
        dialog = AskDialog(self.parent, prompt, secret)
        dialog.exec()
        return dialog.answer

    def info(self, title, text):
        box = QMessageBox(self.parent)
        box.setWindowTitle(title)
        box.setText(shown(text))
        box.exec()


# What the side facts call each console state.
STATE_NAMES = {
    'stock': 'No start-up menu', 'installed': 'Start-up menu installed', 'outdated': 'Start-up menu, update available',
    'dormant': 'Menu switched off (after an update)', 'partial': 'Menu switched off', 'damaged': "Can't start",
    'unknown': 'Firmware this app does not change',
}
UP_TO_DATE = 'Start-up menu, up to date'
CHANGED_TEXT = 'Changed: check it again'
RESULT_TONES = {'ok': 'ok', 'warn': 'update', 'error': 'error'}


# ---- the window -----------------------------------------------------------------------------
class MainWindow(QWidget):
    finished = Signal(int)

    def __init__(self, cm, manager_factory=None, lock_factory=None, dialogs=None, root=ROOT, frameless=True,
                 log_folder=None, detect=None, updater_running=None, clock=time.monotonic, settings_path=None,
                 hardware_gate=None, guide_path=None, guide_session=None, sound_image=None, pilot=None,
                 pilot_open=None, pilot_choose=None, jellyfin_page=None, link_image=None, link_guide_path=None):
        """`detect`: a callable giving how many consoles are plugged in (USB enumeration only).
        With it, the window notices a real plug-in and checks the console by itself (see
        poll_console); the real window passes one, tests pass a simulated one or none.
        `updater_running`: is ModRetro's updater running (default: the process list).
        `settings_path`: the private settings file (the remembered AI choice); `hardware_gate`:
        the hardware usage rule for AI-started writes (default: scripts/hardware_preflight.py).
        The guided sound setup: `guide_path` its private progress file (default next to the
        settings), `guide_session` the run its rehearsal belongs to (default chromatic_fpga's
        per-process SESSION), `sound_image` what gives the sound image's short name or None
        (default chromatic_fpga.media_text, read once when first needed).
        The guided Pocket link setup: `link_image` what gives the reviewed 18.41 image's short name
        or None while it is not cleared (default chromatic_fpga.link_ready_text), `link_guide_path`
        its private progress file (default next to the sound guide's).
        The Remote Pilot tab: `pilot` a remote_pilot_link.PilotLink, or a callable given the
        private settings that makes one (the real window passes PilotLink.for_app); None: an
        inert one that finds, starts and contacts nothing (tests and previews). `pilot_open`
        opens the viewer ('window' or 'browser', url); `pilot_choose` asks for its folder.
        `jellyfin_page`: options for the Jellyfin page (JellyfinDialog: path, tester, finder, saver,
        loader); tests and previews pass a temporary file and stand-ins, never the private file."""
        super().__init__()
        self.jellyfin_options = dict(jellyfin_page or {})
        self.jellyfin_dialog = None
        self._summary_before = {}
        if guide_session is None or sound_image is None or link_image is None:
            import chromatic_fpga
            guide_session = chromatic_fpga.SESSION if guide_session is None else guide_session
            sound_image = sound_image or chromatic_fpga.media_text
            link_image = link_image or chromatic_fpga.link_ready_text
        if guide_path is None:
            guide_path = (Path(settings_path).parent / Path(SOUND_GUIDE_FILE).name if settings_path
                          else data_folder(root) / SOUND_GUIDE_FILE)
        if link_guide_path is None:
            link_guide_path = Path(guide_path).parent / Path(LINK_GUIDE_FILE).name
        # Two guides, one at a time: self.sound is the one on screen (or the last one shown).
        self.sound_guide = SoundGuide(guide_path, guide_session)
        self.link_guide = LinkGuide(link_guide_path, guide_session, confirmer=self._confirm_link_try,
                                    try_confirmed=self._link_try_confirmed)
        # Update the Pocket link in one step: no file, no questions; it reads the last status summary.
        self.link_update = LinkUpdateGuide(None, guide_session, facts=self._update_facts)
        self.sound = self.sound_guide
        self.sound_image, self._sound_image_text = sound_image, ...  # ... : not read yet
        self.link_image, self._link_image_text = link_image, ...
        self._guide_job = None
        self.cm, self.root = cm, Path(root)
        self.detect, self.consoles = detect, None
        self.updater_running = updater_running or modretro_updater_running
        self.clock = clock
        self._absent_since, self._plug_armed, self._quiet_until, self._stale = None, False, 0.0, False
        self.service = ManagerService(cm, manager_factory, lock_factory, log_folder, data_folder(root), host='window',
                                      settings_path=settings_path, hardware_gate=hardware_gate)
        self.running, self.next_text, self._confirming = None, '', False
        self.dialogs = dialogs or Dialogs(self)
        if isinstance(self.dialogs, Dialogs):
            self.dialogs.lines = lambda: list(self.run_lines)
            self.dialogs.release = lambda: (self.peek_release() or (None, None))[1]
        self.lines, self.run_lines = [], []
        self.last_code = None
        self._drag = None
        self.frameless = frameless
        self.bridge = Bridge(self)
        self.bridge.event.connect(self.on_service)  # a bound method: always runs on the window's thread
        self.bridge.question.connect(self.on_question)
        self._answer, self._answered = '', threading.Event()
        self.service.listeners.append(lambda event, data: self.bridge.event.emit(event, data))
        self.service.asker = self.ask_from_job
        self.service.attach_confirmer()  # write requests from the API are put to the person here
        if callable(pilot) and not isinstance(pilot, pilot_link.PilotLink):
            pilot = pilot(self.service.settings)
        self.pilot = pilot or pilot_link.PilotLink.inert()
        self.service.port_handoff = self.pilot.handoff  # every job: Remote Pilot lets go of USB first
        self.pilot_open = pilot_open or self.open_viewer
        self.pilot_choose = pilot_choose or self._choose_pilot_folder
        self._pilot = {'launch': '', 'status': None, 'since': None, 'open': None, 'busy': False, 'at': None}
        self.bridge.pilot.connect(self.on_pilot)
        self.pilot_timer = QTimer(self)
        self.pilot_timer.timeout.connect(self.pilot_poll)
        self.setObjectName('Window')
        self.setWindowTitle(APP_NAME)
        if frameless:
            self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window)
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMinimumSize(*WINDOW)  # resizable like a normal window, never smaller than this
        self.resize(*WINDOW)
        self.setMouseTracking(True)   # resize cursors on the edges
        Privacy.on = True  # private details are hidden at every start
        self.log_lines = deque(maxlen=2000)  # the log panel's real lines; it shows them masked while hidden
        self.tip_mask = TipMask(self)
        if QApplication.instance() is not None:
            QApplication.instance().installEventFilter(self.tip_mask)
        self._build()
        self.set_privacy(True)
        self.show_tab('console')
        self.refresh_pending()
        self.refresh_ai()
        if self.sound_guide.resume_on_open():  # a sound setup left unfinished by an earlier run
            self.add_line('The sound setup was not finished when Chromatic Manager closed. It offers to start again.')
            self.show_guide()
        elif self.link_guide.resume_on_open():  # likewise the Pocket link setup
            self.sound = self.link_guide
            self.add_line('The Pocket link setup was not finished when Chromatic Manager closed. It offers to go on.')
            self.show_guide()
        stale = self.pilot.handoff.clear_stale(self.service.pending())  # a hold an earlier run left behind
        if stale:
            self.add_line(stale)
        self.detect_timer = QTimer(self)
        self.detect_timer.timeout.connect(self.poll_console)
        if self.detect is not None:
            self.detect_timer.start(1500)
            QTimer.singleShot(0, self.poll_console)  # a console already plugged in is checked at once

    # layout ---------------------------------------------------------------------------
    TITLES = {'console': 'START-UP MENU', 'pilot': 'REMOTE PILOT', 'recovery': 'FIX A PROBLEM', 'settings': 'SETTINGS',
              'about': 'ABOUT'}
    GUIDE_TITLE = 'JELLYFIN SOUND'
    SOUND_BUTTON = 'Set up Jellyfin sound'
    LINK_BUTTON = 'Update the Pocket link'  # the one-step update; its first screen offers Step by step
    GUIDE_NAMES = {'link': 'Pocket link setup', 'link_update': 'Pocket link update', 'sound': 'sound setup'}
    UPDATE_SOUND_BUTTON = 'Update sound'
    TAB_NAMES = {'console': 'Home', 'pilot': 'Remote Pilot', 'recovery': 'Fix a problem', 'settings': 'Settings',
                 'about': 'About'}
    TAB_TIPS = {'pilot': 'Remote Pilot: see your Chromatic\'s screen on this PC and press its buttons from here.'}
    TAB_W, TAB_H = 40, 32

    def _build(self):
        """The updater's layout, in our own drawing: tabs and a close box on top; a black body
        with a thick border in the tab's colour; a header band with the title; our pixel
        console on the left; the speech bubble with the page; a bottom row with the log
        toggle and our multicolour name. The frame, band and doodles are painted in
        paintEvent; everything else is widgets."""
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 6, 10, 10)
        outer.setSpacing(0)

        top = QHBoxLayout()
        top.setContentsMargins(18, 0, 6, 0)
        top.setSpacing(3)
        self.tabs = {}
        for key in ('console', 'pilot', 'recovery', 'settings', 'about'):
            tab = button('')
            tab.setProperty('tab', True)
            tab.icon_key, tab.tab_colour = key, theme.TAB_COLOURS[key]
            tab.setCheckable(True)
            tab.setFixedSize(self.TAB_W, self.TAB_H)
            tab.setToolTip(self.TAB_TIPS.get(key, self.TAB_NAMES[key]))
            tab.setAccessibleName(self.TAB_NAMES[key])
            tab.clicked.connect(lambda _=False, k=key: self.show_tab(k))
            top.addWidget(tab, 0, Qt.AlignmentFlag.AlignBottom)
            self.tabs[key] = tab
        top.addSpacing(10)
        self.ai_chip = label('AI ON', 'notice', wrap=False, size=theme.GEOMETRY['small_point_size'], bold=True)
        self.ai_chip.setToolTip('You allowed AI control. It runs the same safety checks you get. Press Stop AI to '
                                'take it back at once.')
        top.addWidget(self.ai_chip, 0, Qt.AlignmentFlag.AlignVCenter)
        self.ai_stop = button('Stop AI', 'stop', size=theme.GEOMETRY['small_point_size'])
        self.ai_stop.setFixedHeight(26)
        self.ai_stop.setToolTip('Takes the AI\'s permission back now. A job it started stops before anything is '
                                'changed, or, if a change is being written, finishes safely first.')
        self.ai_stop.clicked.connect(self.stop_ai)
        top.addWidget(self.ai_stop, 0, Qt.AlignmentFlag.AlignVCenter)
        top.addSpacing(6)
        # The USB hand-off sign: Remote Pilot paused while a job uses the cable, then back.
        self.pilot_chip = label('', 'notice', wrap=False, size=theme.GEOMETRY['small_point_size'], bold=True)
        self.pilot_chip.hide()
        top.addWidget(self.pilot_chip, 0, Qt.AlignmentFlag.AlignVCenter)
        top.addStretch(1)
        # Hide private details: our pixel eye, on at every start.
        self.privacy = button('', 'privacy')
        self.privacy.setCheckable(True)
        self.privacy.setFixedSize(34, 28)
        self.privacy.clicked.connect(lambda: self.set_privacy(not Privacy.on))
        top.addWidget(self.privacy, 0, Qt.AlignmentFlag.AlignVCenter)
        top.addSpacing(6)
        self.close_box = button('', 'close')
        self.close_box.setFixedSize(22, 22)
        self.close_box.setToolTip('Close')
        self.close_box.clicked.connect(self.close)
        if self.frameless:
            minimize = button('', 'link')
            minimize.hide()
        top.addWidget(self.close_box, 0, Qt.AlignmentFlag.AlignVCenter)
        outer.addLayout(top)

        self.body = QWidget()
        body = QVBoxLayout(self.body)
        body.setContentsMargins(14, 12, 14, 8)
        body.setSpacing(8)
        band = QHBoxLayout()
        band.setContentsMargins(10, 4, 90, 4)
        self.title = PixelText('', theme.PALETTE['window'], theme.GEOMETRY['title_scale'])
        band.addWidget(self.title, 1)
        body.addLayout(band)
        # The whole header band moves the window, like a title bar.
        self.title.installEventFilter(self)
        self.body.installEventFilter(self)
        middle = QHBoxLayout()
        middle.setSpacing(4)
        self.console_art = ConsoleArt()
        middle.addWidget(self.console_art, 0, Qt.AlignmentFlag.AlignVCenter)
        self.bubble = Bubble()
        bubble_layout = QVBoxLayout(self.bubble)
        bubble_layout.setContentsMargins(0, 0, 0, 0)
        self.stack = QStackedWidget()
        bubble_layout.addWidget(self.stack)
        middle.addWidget(self.bubble, 1)
        body.addLayout(middle, 1)
        bottom = QHBoxLayout()
        self.log_toggle = button('Show log', 'bottom')
        self.log_toggle.icon_key = 'log'
        self.log_toggle.setToolTip('The log lists every step with the time. Nothing private is in it.')
        self.log_toggle.clicked.connect(lambda: self.set_log_open(self.log.isHidden()))
        bottom.addWidget(self.log_toggle)
        bottom.addStretch(1)
        # The owner's credit: small and subtle, smaller than any button text, beside the logo.
        self.credit = label(CREDIT, 'dim', wrap=False, size=CREDIT_PT)
        self.credit.setObjectName('Credit')
        self.credit.setToolTip(CREDIT)
        bottom.addWidget(self.credit, 0, Qt.AlignmentFlag.AlignVCenter)
        bottom.addSpacing(10)
        self.logo = PixelText(APP_NAME, LOGO_COLOURS, 2, 9)
        self.logo.setToolTip('Chromatic Manager is made for this project. It is not made or endorsed by ModRetro.')
        bottom.addWidget(self.logo, 0, Qt.AlignmentFlag.AlignVCenter)
        body.addLayout(bottom)
        self.log = QPlainTextEdit()
        self.log.setObjectName('Log')
        self.log.setReadOnly(True)
        mono = theme.body_font(theme.GEOMETRY['small_point_size'])
        self.log.setFont(mono)
        self.log.setFixedHeight(theme.GEOMETRY['window_log'][1] - theme.GEOMETRY['window'][1] - 10)
        self.log.setMaximumBlockCount(2000)
        self.log.hide()  # collapsed by default
        body.addWidget(self.log)
        outer.addWidget(self.body, 1)

        self.buttons = {}
        self.pages = {}
        self.pages['console'] = self._home_page()
        self.pages['pilot'] = self._pilot_page()
        self.pages['recovery'] = self._fix_page()
        self.pages['settings'] = self._settings_page()
        self.pages['about'] = self._about_page()
        self.pages['working'] = self._working_page()
        self.pages['result'] = self._result_page()
        self.pages['guide'] = self._guide_page()
        self.pages['owner'] = self._owner_page()
        for page in self.pages.values():
            self.stack.addWidget(page)
        for card in (self.status_card, self.work_card, self.result_card, self.guide_card, self.pilot_card):
            card.on_tone = lambda tone, c=card: self._card_tone(c, tone)

    def _card_tone(self, card, tone):
        """The console drawing shows the tone of the card on screen."""
        if self.stack.currentWidget() is card.parentWidget() or card is self.status_card and not self.busy():
            self.console_art.set_tone(tone)

    def paintEvent(self, event):
        """The frame: a thick border in the tab's colour round a black body with faint line
        doodles (our own), and the header band with a dithered fade at its right end."""
        p = QPainter(self)
        accent = colour(theme.TAB_COLOURS.get(getattr(self, 'current_tab', 'console'), 'tab_home'))
        r = self.body.geometry().adjusted(0, 0, 0, 0)
        outer = r.adjusted(-2, -2, 2, 2)
        p.fillRect(outer.adjusted(3, 0, -3, 0), accent)
        p.fillRect(outer.adjusted(0, 3, 0, -3), accent)
        p.fillRect(outer.adjusted(1, 1, -1, -1), accent)
        inner = r.adjusted(4, 4, -4, -4)
        p.fillRect(inner, colour('window'))
        p.fillRect(inner.adjusted(2, 2, -2, -2), colour('window'))
        self._doodles(p, inner)
        # inner dark rule, as in the updater's frame
        pen = QPen(colour('frame_shadow'))
        pen.setWidth(1)
        p.setPen(pen)
        p.drawRect(inner.adjusted(2, 2, -3, -3))
        # header band
        band = QRect(inner.x() + 6, self.title.mapTo(self, self.title.rect().topLeft()).y() - 4,
                     inner.width() - 12, self.title.height() + 8)
        p.fillRect(band, accent)
        cell = 4
        steps = 18
        for col in range(steps):
            for row in range(band.height() // cell + 1):
                x = band.right() - (steps - col) * cell
                density = col / steps
                if ((row + col) % 2 == 0 and density > 0.25) or density > 0.75 or (
                        (row * 3 + col) % 5 == 0 and density > 0.1):
                    p.fillRect(x, band.y() + row * cell, cell, min(cell, band.bottom() - band.y() - row * cell + 1),
                               colour('window'))
        p.end()

    def _doodles(self, p, area):
        """Faint line doodles behind everything: our own simple shapes (rings, stars, zigzags,
        plus signs, little notes), placed from a fixed seed so they never move."""
        import random
        rnd = random.Random(1107)
        pen = QPen(colour('doodle'))
        pen.setWidth(2)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        for _ in range(26):
            x = area.x() + rnd.randint(4, max(5, area.width() - 40))
            y = area.y() + rnd.randint(30, max(31, area.height() - 30))
            size = rnd.randint(10, 26)
            shape = rnd.randint(0, 4)
            if shape == 0:
                p.drawEllipse(x, y, size, size)
            elif shape == 1:
                p.drawLine(x, y + size // 2, x + size, y + size // 2)
                p.drawLine(x + size // 2, y, x + size // 2, y + size)
            elif shape == 2:
                p.drawPolyline([QPointF(x + i * size / 4, y + (size / 3 if i % 2 else 0)) for i in range(5)])
            elif shape == 3:
                import math
                points = [QPointF(x + size / 2 + math.cos(math.pi / 2 + i * 4 * math.pi / 5) * size / 2,
                                  y + size / 2 - math.sin(math.pi / 2 + i * 4 * math.pi / 5) * size / 2)
                          for i in range(6)]
                p.drawPolyline(points)
            else:
                p.drawRect(x, y, size, size * 2 // 3)

    def set_privacy(self, hide):
        """Hide (the default) or show private details on screen. Display only: every label,
        headline, tooltip and the log panel are drawn again from their real text."""
        Privacy.set(hide)
        self.privacy.setChecked(Privacy.on)
        self.privacy.setToolTip(PRIVACY_TIP_ON if Privacy.on else PRIVACY_TIP_OFF)
        self.privacy.setAccessibleName('Private details hidden' if Privacy.on else 'Private details showing')
        self.privacy.update()
        bar = self.log.verticalScrollBar()
        at_end = bar.value() >= bar.maximum()
        self.log.setPlainText('\n'.join(shown(line) for line in self.log_lines))
        if at_end:
            bar.setValue(bar.maximum())

    def log_line(self, text):
        """One line in the log panel (the time first), shown masked while private details are hidden."""
        line = f'{time.strftime("%H:%M:%S")}  {text}'
        self.log_lines.append(line)
        self.log.appendPlainText(shown(line))

    def set_log_open(self, open_):
        """Show or hide the log panel (also from the API, for the AI to show the owner). The
        window grows to make room for it, and shrinks back."""
        self.log.setVisible(bool(open_))
        self.log_toggle.setText('Hide log' if open_ else 'Show log')
        grow = WINDOW_LOG[1] - WINDOW[1]
        if open_:  # room for the log, keeping any size the owner chose
            self.setMinimumSize(*WINDOW_LOG)
            if self.height() < WINDOW_LOG[1]:
                self.resize(self.width(), WINDOW_LOG[1])
        else:
            self.setMinimumSize(*WINDOW)
            self.resize(self.width(), max(WINDOW[1], self.height() - grow))
        with self.service._lock:
            self.service.ui['log_panel_open'] = bool(open_)

    def _page(self, spacing=8):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(spacing)
        return page, layout

    def _home_page(self):
        """Home: one plain status, at most one main button, a few small extras under More, and
        the console's facts behind Show details (off by default)."""
        page, layout = self._page()
        self.home_main = QWidget()
        main = QVBoxLayout(self.home_main)
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(8)
        self.status_card = StatusCard()
        main.addStretch(1)
        main.addWidget(self.status_card)
        # What the console chip (FPGA) runs, as the start-up menu reported it at the last check:
        # one short line ("Sound: on"), the details in its tooltip; hidden when there is nothing to say.
        self.sound_line = label('', 'ok', size=theme.GEOMETRY['small_point_size'])
        self.sound_line.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.sound_line.hide()
        main.addWidget(self.sound_line)
        # The console's Wi-Fi and Jellyfin, as the last check found them: a
        # sign, one plain line and, when something needs doing, the one button that does it.
        self.setup_row = QWidget()
        setup = QHBoxLayout(self.setup_row)
        setup.setContentsMargins(0, 0, 0, 0)
        setup.setSpacing(6)
        setup.addStretch(1)
        self.setup_mark = Sign('info')
        setup.addWidget(self.setup_mark, 0, Qt.AlignmentFlag.AlignVCenter)
        self.setup_text = label('', 'value', wrap=False, size=theme.GEOMETRY['small_point_size'])
        setup.addWidget(self.setup_text, 0, Qt.AlignmentFlag.AlignVCenter)
        self.setup_button = button('', 'go', size=theme.GEOMETRY['small_point_size'])
        self.setup_button.setFixedHeight(26)
        self.setup_button.clicked.connect(self._setup_clicked)
        setup.addWidget(self.setup_button, 0, Qt.AlignmentFlag.AlignVCenter)
        setup.addStretch(1)
        self.setup_row.hide()
        self.setup_now = None
        main.addWidget(self.setup_row)
        self.guide_head, self.guide_steps = self.status_card.head, self.status_card.sub
        self.primary = button('', 'go', size=theme.GEOMETRY['base_point_size'] + 3)
        self.primary.setFixedHeight(42)
        self.primary.setMinimumWidth(170)
        self.primary.clicked.connect(self._primary_clicked)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self.primary, 3)
        row.addStretch(1)
        main.addLayout(row)
        self.change_setup = QCheckBox('Also change Wi-Fi and Jellyfin')
        self.change_setup.setFont(theme.body_font(theme.GEOMETRY['small_point_size']))
        self.change_setup.setToolTip('Without this, a console that already has Wi-Fi and Jellyfin keeps them. One '
                                     'that has none gets them set up anyway. Your Wi-Fi comes from your private '
                                     'Wi-Fi file, or you type it in.')
        main.addWidget(self.change_setup, 0, Qt.AlignmentFlag.AlignHCenter)
        main.addStretch(1)
        more_row = QHBoxLayout()
        more_row.setSpacing(5)
        more = label('MORE', 'dim', wrap=False, size=theme.GEOMETRY['small_point_size'], bold=True)
        more_row.addWidget(more)
        for action in ACTIONS:
            if action.tab != 'console':
                continue
            widget = button(action.label, '', size=theme.GEOMETRY['small_point_size'])
            widget.setFixedHeight(30)
            widget.setToolTip(action.help)
            widget.clicked.connect(lambda _=False, k=action.key: self.start(k))
            more_row.addWidget(widget, 1)
            self.buttons[action.key] = widget
        main.addLayout(more_row)
        layout.addWidget(self.home_main, 1)
        self.details = QFrame()
        self.details.setObjectName('Details')
        grid = QGridLayout(self.details)
        grid.setContentsMargins(10, 8, 10, 8)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(5)
        small = theme.GEOMETRY['small_point_size']
        self.summary_state, self.summary_boots = label('Not checked yet', 'value', size=small), label('--', 'value', size=small)
        self.summary_stock, self.summary_menu = label('--', 'value', size=small), label('--', 'value', size=small)
        self.summary_setup = label('--', 'value', size=small)
        for n, (name, widget) in enumerate((('Console', self.summary_state), ('Starts into', self.summary_boots),
                                            ('Original firmware', self.summary_stock),
                                            ('Start-up menu', self.summary_menu),
                                            ('Wi-Fi and Jellyfin', self.summary_setup))):
            grid.addWidget(label(name, 'dim', wrap=False, size=small), n, 0, Qt.AlignmentFlag.AlignTop)
            grid.addWidget(widget, n, 1)
        grid.setColumnStretch(1, 1)
        self.pending_note = label('', 'error', size=small)
        grid.addWidget(self.pending_note, 5, 0, 1, 2)
        self.details.hide()
        layout.addWidget(self.details, 1)
        self.details_toggle = button('Show details', 'link', size=theme.GEOMETRY['small_point_size'])
        self.details_toggle.setToolTip('What this app last saw on your console.')
        self.details_toggle.clicked.connect(self._toggle_details)
        layout.addWidget(self.details_toggle, 0, Qt.AlignmentFlag.AlignRight)
        self.step_now = guide({}, False, None)
        return page

    def _toggle_details(self):
        """The facts take the bubble's place while shown (the window keeps its size)."""
        show = self.details.isHidden()
        self.details.setVisible(show)
        self.home_main.setVisible(not show)
        self.details_toggle.setText('Hide details' if show else 'Show details')

    def _toggle(self, widget, toggle, noun):
        widget.setVisible(widget.isHidden())
        toggle.setText(f'Hide {noun}' if widget.isVisible() else f'Show {noun}')

    def _fix_page(self):
        page, layout = self._page(6)
        layout.addWidget(label('Only if something is wrong. Point at a button to read what it does.', 'notice',
                               size=theme.GEOMETRY['small_point_size']))
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)
        # The one-button guided sound setup: the console chip jobs below, in
        # order, with what to look at on the console between them.
        self.fix_sound = button(self.SOUND_BUTTON, 'go', size=theme.GEOMETRY['small_point_size'] + 0.5)
        self.fix_sound.setFixedHeight(32)
        self.fix_sound.setToolTip(SOUND_TIP)
        self.fix_sound.clicked.connect(lambda: self.open_sound_guide())
        grid.addWidget(self.fix_sound, 0, 0)
        # Update the Pocket link (the combined image 18.41) in one step. Its jobs have no buttons of
        # their own; its first screen also offers the question-based setup (Step by step).
        self.fix_link = button(self.LINK_BUTTON, 'go', size=theme.GEOMETRY['small_point_size'] + 0.5)
        self.fix_link.setFixedHeight(32)
        self.fix_link.setToolTip(LINK_UPDATE_TIP)
        self.fix_link.clicked.connect(lambda: self.open_link_update())
        grid.addWidget(self.fix_link, 0, 1)
        n = 2
        for action in ACTIONS:
            if action.tab != 'recovery' or action.key in GUIDE_ONLY_KEYS + OWNER_APPROVE_KEYS:
                continue
            widget = button(action.label, action.kind, size=theme.GEOMETRY['small_point_size'] + 0.5)
            widget.setFixedHeight(32)
            widget.setToolTip(f'{ROW_TEXT.get(action.key, "")} {action.help}'.strip())
            widget.clicked.connect(lambda _=False, k=action.key: self.start(k))
            grid.addWidget(widget, n // 2, n % 2)
            self.buttons[action.key] = widget
            n += 1
        layout.addLayout(grid)
        layout.addStretch(1)
        return page

    def _settings_page(self):
        """Settings: the owner's AI choice, what it is now and a button to forget it."""
        page, layout = self._page(8)
        layout.addWidget(label('AI CONTROL', 'value', size=theme.GEOMETRY['base_point_size'] + 1, bold=True))
        explain = label('Can an AI (like Claude) install updates and use this app for you? The first time it asks, '
                        'this app asks you.', 'notice', size=theme.GEOMETRY['small_point_size'])
        explain.setToolTip('Allowed, the AI runs the same jobs you can, with the same safety checks, and has to '
                           'pass the project\'s usage check before it changes anything. It never sees your Wi-Fi '
                           'passwords, Jellyfin sign-in or backups. Not allowed, you approve every change.')
        layout.addWidget(explain)
        self.ai_remembered = label('', 'value', size=theme.GEOMETRY['small_point_size'])
        self.ai_now = label('', 'value', size=theme.GEOMETRY['small_point_size'])
        layout.addWidget(self.ai_remembered)
        layout.addWidget(self.ai_now)
        row = QHBoxLayout()
        self.ai_forget = button('Forget my choice', '')
        self.ai_forget.setFixedHeight(32)
        self.ai_forget.setToolTip('The next time an AI asks, this app asks you again.')
        self.ai_forget.clicked.connect(self.forget_ai)
        self.ai_stop_settings = button('Stop AI now', 'stop')
        self.ai_stop_settings.setFixedHeight(32)
        self.ai_stop_settings.setToolTip(self.ai_stop.toolTip())
        self.ai_stop_settings.clicked.connect(self.stop_ai)
        row.addWidget(self.ai_forget, 1)
        row.addWidget(self.ai_stop_settings, 1)
        layout.addLayout(row)
        # Jellyfin on this PC: the server and sign-in Chromatic Manager uses to
        # give the console its own Jellyfin sign-in, on a page instead of a hand-edited file.
        layout.addSpacing(6)
        layout.addWidget(label('JELLYFIN ON THIS PC', 'value', size=theme.GEOMETRY['base_point_size'] + 1, bold=True))
        state = QHBoxLayout()
        self.jellyfin_mark = Sign('info')
        state.addWidget(self.jellyfin_mark, 0, Qt.AlignmentFlag.AlignVCenter)
        self.jellyfin_state = label('', 'value', size=theme.GEOMETRY['small_point_size'])
        state.addWidget(self.jellyfin_state, 1)
        layout.addLayout(state)
        self.jellyfin_open = button('Jellyfin server', 'go')
        self.jellyfin_open.setFixedHeight(32)
        self.jellyfin_open.setToolTip('Your Jellyfin server\'s address (Find servers), user name and password, '
                                      'tested with yes/no results and kept in this PC\'s private file. Then New '
                                      'Jellyfin sign-in gives your Chromatic its own sign-in.')
        self.jellyfin_open.clicked.connect(self.open_jellyfin_page)
        layout.addWidget(self.jellyfin_open, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addStretch(1)
        self.refresh_jellyfin_state()
        return page

    def _about_page(self):
        page, layout = self._page(6)
        layout.addWidget(label(APP_NAME, 'value', size=theme.GEOMETRY['base_point_size'] + 2, bold=True))
        layout.addWidget(label('Installs, updates and repairs the start-up menu on your Chromatic, and backs it up. '
                               'Games, saves and cartridges are never changed.', 'value',
                               size=theme.GEOMETRY['small_point_size']))
        layout.addWidget(label('Not made or endorsed by ModRetro. Our own drawings; no ModRetro images, logos or '
                               'fonts.', 'dim', size=theme.GEOMETRY['small_point_size']))
        self.about_credit = label(CREDIT, 'dim', wrap=False, size=CREDIT_PT)
        layout.addWidget(self.about_credit)
        for text, target in (('Open the project folder', self.root),
                             ('Open the backups folder', data_folder(self.root) / 'chromatic-manager-backups'),
                             ('Read the guide', self.root / 'docs/Install-with-Chromatic-Manager.md'),
                             ('Licences', None)):
            link = button(text, 'link')
            if target is None:
                link.clicked.connect(self.show_licences)
            else:
                link.setToolTip(str(target))
                link.clicked.connect(lambda _=False, t=target: QDesktopServices.openUrl(QUrl.fromLocalFile(str(t))))
            layout.addWidget(link, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addStretch(1)
        return page

    def _working_page(self):
        page, layout = self._page(6)
        self.work_card = StatusCard(12, pixel=False)
        self.work_card.set('busy', '')
        self.work_title = self.work_card.head
        self.work_todo = self.work_card.sub
        layout.addWidget(self.work_card)
        self.banner = QFrame()
        self.banner.setObjectName('Banner')
        banner_layout = QVBoxLayout(self.banner)
        banner_layout.setContentsMargins(8, 4, 8, 4)
        banner_layout.setSpacing(0)
        self.banner_head = label('', 'banner', size=theme.GEOMETRY['small_point_size'], bold=True)
        self.banner_text = label('', 'banner', size=theme.GEOMETRY['small_point_size'])
        self.banner_text.hide()  # the head says it; the line under it is in the tooltip
        banner_layout.addWidget(self.banner_head)
        banner_layout.addWidget(self.banner_text)
        self.banner.hide()
        layout.addWidget(self.banner)
        bar_row = QHBoxLayout()
        bar_row.addStretch(1)
        self.bar = SegmentBar()
        bar_row.addWidget(self.bar)
        self.work_percent = label('', 'value', wrap=False, size=12, bold=True)
        self.work_percent.setMinimumWidth(48)
        bar_row.addWidget(self.work_percent)
        bar_row.addStretch(1)
        layout.addLayout(bar_row)
        self.work_usage = label('', 'notice', size=theme.GEOMETRY['small_point_size'], bold=True)
        self.work_usage.setToolTip('The AI reports its own usage; this app cannot check it. It refuses a reading that '
                                   'is missing, older than 5 minutes, or 90% used or more.')
        self.work_usage.hide()
        layout.addWidget(self.work_usage)
        self.work_pilot = label('', 'notice', size=theme.GEOMETRY['small_point_size'])
        self.work_pilot.setToolTip(PILOT_PAUSED_TIP)
        self.work_pilot.hide()
        layout.addWidget(self.work_pilot)
        self.work_phase = label('', 'value', size=theme.GEOMETRY['small_point_size'])
        self.work_phase.hide()  # the step line below says it
        layout.addWidget(self.work_phase)
        self.work_step = label('', 'value', size=theme.GEOMETRY['small_point_size'])
        layout.addWidget(self.work_step)
        self.work_time = label('', 'dim', size=theme.GEOMETRY['small_point_size'])
        layout.addWidget(self.work_time)
        self.work_line = label('', 'dim', size=theme.GEOMETRY['small_point_size'])
        self.work_line.hide()  # the manager's last message; the log shows it (kept for tests and --auto)
        layout.addWidget(self.work_line)
        layout.addStretch(1)
        stop_row = QHBoxLayout()
        stop_row.addStretch(1)
        self.work_stop = button('Stop the AI', 'stop')
        self.work_stop.setFixedHeight(34)
        self.work_stop.setToolTip(self.ai_stop.toolTip())
        self.work_stop.clicked.connect(self.stop_ai)
        self.work_stop.hide()
        stop_row.addWidget(self.work_stop, 2)
        stop_row.addStretch(1)
        layout.addLayout(stop_row)
        self.clock_timer = QTimer(self)
        self.clock_timer.timeout.connect(self.refresh_work)
        return page

    # what the running job needs the owner to do now ------------------------------------------
    OWNER_THANKS_MS = 2500  # how long the green "Done, thank you" stays before the working screen returns

    def _owner_page(self):
        """The owner-action sign (OwnerBanner) as its own screen in the speech bubble: while a job
        waits for the owner it replaces the working screen, so the request cannot be missed."""
        page, layout = self._page(0)
        self.owner_banner = OwnerBanner()
        self.owner_banner.ok.clicked.connect(self.owner_ok)
        self.owner_banner.stop.clicked.connect(self.stop_ai)
        layout.addWidget(self.owner_banner)
        self.owner_timer = QTimer(self)
        self.owner_timer.timeout.connect(self.refresh_owner)
        self._owner, self._owner_after, self.owner_calls = None, None, 0
        return page

    def owner_left(self):
        """Whole seconds left for the owner action on screen (None: no time limit, or none shown)."""
        action = self._owner
        if action is None or action['state'] != owner_actions.WAITING or action['seconds'] is None:
            return None
        return max(0, int(math.ceil(action['seconds'] - (self.clock() - action['at']) - 1e-9)))

    def refresh_owner(self):
        """Runs every quarter second while an owner action waits: the countdown and the blink."""
        action = self._owner
        if action is None or action['state'] != owner_actions.WAITING:
            self.owner_timer.stop()
            return
        self.owner_banner.set_left(self.owner_left(), action['seconds'])
        self._owner_ticks = getattr(self, '_owner_ticks', 0) + 1
        if self._owner_ticks % 2 == 0:
            self.owner_banner.tick()

    def on_owner(self, event):
        """The job announced, or ended, something the owner must do (the service's 'owner' event)."""
        state = event.get('state')
        if state == owner_actions.WAITING:
            if not self.busy():
                return
            self._owner, self._owner_after = dict(event, at=self.clock()), None
            self.owner_banner.show_action(self._owner)
            self.owner_banner.stop.setVisible(not self.work_stop.isHidden())
            self.owner_banner.set_left(self.owner_left(), self._owner['seconds'])
            self.owner_timer.start(250)
            self.stack.setCurrentWidget(self.pages['owner'])
            self.console_art.set_tone('update')
            self.call_owner(event.get('kind') == owner_actions.ACTION)
            return
        if self._owner is None or event.get('id') != self._owner.get('id'):
            return
        number = self._owner['id']
        self.owner_timer.stop()
        if state == owner_actions.CLOSED:
            self.owner_leave(number)
            return
        self._owner.update(state=state, note=event.get('note', ''), next=event.get('next', ''))
        self.owner_banner.show_action(self._owner)
        self.owner_banner.stop.hide()
        if self.stack.currentWidget() is self.pages['owner']:
            self.console_art.set_tone(self.owner_banner.tone())
        if state == owner_actions.DONE:
            QTimer.singleShot(self.OWNER_THANKS_MS, lambda: self.owner_leave(number))

    def call_owner(self, sound=True):
        """Bring the request in front of the owner: the window out of the taskbar and on top, its
        taskbar button flashing, and one beep."""
        self.owner_calls += 1
        try:
            if self.isMinimized():
                self.showNormal()
            if self.isVisible():
                self.raise_()
                QApplication.alert(self, 0)
                if sound:
                    QApplication.beep()
        except Exception:
            pass  # calling for attention must never disturb the job

    def owner_leave(self, number):
        """The sign is no longer needed (done and thanked, or a question answered): back to the
        working screen while the job goes on. A timed-out sign stays until OK."""
        action = self._owner
        if action is None or action['id'] != number or action['state'] == owner_actions.TIMEOUT:
            return
        self._owner = None
        self.owner_timer.stop()
        if self.stack.currentWidget() is self.pages['owner'] and self.busy():
            self.stack.setCurrentWidget(self.pages['working'])
            self.console_art.set_tone('busy')

    def owner_ok(self):
        """OK on the red "Time ran out" sign: on to how the job ended (its result, or the guide's screen)."""
        after, self._owner, self._owner_after = self._owner_after, None, None
        if self.busy() or self.stack.currentWidget() is not self.pages['owner']:
            return
        page, tone = after or (self.pages['result'], self.result_card.tone)
        self.stack.setCurrentWidget(page)
        self.console_art.set_tone(tone)
        self.update()

    def _result_page(self):
        page, layout = self._page(8)
        self.result_card = StatusCard()
        self.result_head, self.result_next = self.result_card.head, self.result_card.sub
        layout.addStretch(1)
        layout.addWidget(self.result_card)
        self.result_todo = label('', 'value', size=theme.GEOMETRY['base_point_size'], bold=True)
        self.result_todo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.result_todo)
        row = QHBoxLayout()
        row.addStretch(1)
        self.result_fix = button('Fix a problem', 'error')
        self.result_fix.setFixedHeight(36)
        self.result_fix.setToolTip('Opens the Fix a problem tab, with the buttons that put things right.')
        self.result_fix.clicked.connect(lambda: self.show_tab('recovery'))
        self.result_ok = button('OK', 'ok', size=theme.GEOMETRY['base_point_size'] + 2)
        self.result_ok.setFixedHeight(36)
        self.result_ok.clicked.connect(lambda: self.show_tab(self.current_tab))
        # The guided official update's last step: ModRetro's updater removes
        # the sound image, so the result offers the sound setup again.
        self.result_sound = button(self.SOUND_BUTTON, 'go')
        self.result_sound.setFixedHeight(36)
        self.result_sound.setToolTip(SOUND_TIP)
        self.result_sound.clicked.connect(self._result_sound_clicked)
        self.result_sound.hide()
        row.addWidget(self.result_fix, 2)
        row.addWidget(self.result_sound, 3)
        row.addWidget(self.result_ok, 2)
        row.addStretch(1)
        layout.addLayout(row)
        layout.addStretch(1)
        self.result_text = label('', 'dim', size=theme.GEOMETRY['small_point_size'] - 0.5)
        self.result_text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.result_text.hide()
        layout.addWidget(self.result_text)
        self.result_toggle = button('Show details', 'link', size=theme.GEOMETRY['small_point_size'])
        self.result_toggle.clicked.connect(lambda: self._toggle(self.result_text, self.result_toggle, 'details'))
        layout.addWidget(self.result_toggle, 0, Qt.AlignmentFlag.AlignRight)
        return page

    def _pilot_page(self):
        """Remote Pilot: one plain status with its three steps, one big button and a smaller
        one for the browser. Opening the tab starts Remote Pilot if it is not running."""
        page, layout = self._page(6)
        layout.addStretch(1)
        self.pilot_card = StatusCard()
        layout.addWidget(self.pilot_card)
        steps_row = QHBoxLayout()
        steps_row.addStretch(1)
        self.pilot_steps = PilotSteps()
        steps_row.addWidget(self.pilot_steps)
        steps_row.addStretch(1)
        layout.addLayout(steps_row)
        bar_row = QHBoxLayout()
        bar_row.addStretch(1)
        self.pilot_bar = SegmentBar()
        self.pilot_bar.setToolTip('Working: Remote Pilot is starting or looking for your Chromatic.')
        self.pilot_bar.hide()
        bar_row.addWidget(self.pilot_bar)
        bar_row.addStretch(1)
        layout.addLayout(bar_row)
        row = QHBoxLayout()
        row.addStretch(1)
        self.pilot_primary = button('Open Remote Pilot', 'go', size=theme.GEOMETRY['base_point_size'] + 3)
        self.pilot_primary.setFixedHeight(42)
        self.pilot_primary.setMinimumWidth(190)
        self.pilot_primary.clicked.connect(self._pilot_primary_clicked)
        row.addWidget(self.pilot_primary, 3)
        self.pilot_browser = button('Open in browser', '', size=theme.GEOMETRY['small_point_size'])
        self.pilot_browser.setFixedHeight(32)
        self.pilot_browser.setToolTip('Opens the same Remote Pilot page in your usual web browser instead. It starts '
                                      'Remote Pilot first if it is not running.')
        self.pilot_browser.clicked.connect(lambda: self.pilot_launch('browser'))
        row.addWidget(self.pilot_browser, 2)
        row.addStretch(1)
        layout.addLayout(row)
        self.pilot_where = label('', 'dim', size=theme.GEOMETRY['small_point_size'])
        self.pilot_where.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.pilot_where)
        layout.addStretch(1)
        links = QHBoxLayout()
        links.setSpacing(12)
        self.pilot_folder = button('Open its folder', 'link', size=theme.GEOMETRY['small_point_size'])
        self.pilot_folder.setToolTip('Opens Remote Pilot\'s own folder on this PC.')
        self.pilot_folder.clicked.connect(self._open_pilot_folder)
        self.pilot_pick = button('Choose its folder', 'link', size=theme.GEOMETRY['small_point_size'])
        self.pilot_pick.setToolTip('Tell this app where Remote Pilot is, if it is in another folder. Pick the '
                                   'folder that has receiver.py in it.')
        self.pilot_pick.clicked.connect(self.pilot_find)
        self.pilot_stop = button('Stop Remote Pilot', 'link', size=theme.GEOMETRY['small_point_size'])
        self.pilot_stop.setToolTip('Closes the Remote Pilot this app started. It also closes by itself when you close '
                                   'Chromatic Manager. A Remote Pilot you started yourself is never closed.')
        self.pilot_stop.clicked.connect(self.pilot_stop_clicked)
        links.addWidget(self.pilot_folder)
        links.addWidget(self.pilot_pick)
        links.addStretch(1)
        links.addWidget(self.pilot_stop)
        layout.addLayout(links)
        self._pilot_view = None
        self._pilot_where = None
        self.refresh_pilot()
        return page

    def pilot_where_kind(self):
        """'window' (Qt WebEngine), 'app' (an Edge or Chrome window of its own) or 'browser'."""
        if self._pilot_where is None:
            self._pilot_where = ('window' if pilot_link.webengine_available() else
                                 'app' if pilot_link.app_browser() else 'browser')
        return self._pilot_where

    def refresh_pilot(self):
        """The tab from what is known now: no network here (the poll brings the status)."""
        state = self._pilot
        found = self.pilot.home() is not None
        status = state['status']
        looking = 0.0 if state['since'] is None else self.clock() - state['since']
        hold = (status or {}).get('usbHold') or ((status or {}).get('transport') or {}).get('usbHold')
        key = pilot_link.view_key(found, state['launch'], status, looking, hold)
        tone, head, sub, step, busy = pilot_link.VIEWS[key]
        self._pilot_view = key
        tip = {'paused': PILOT_PAUSED_TIP, 'paused_kept': PILOT_PAUSED_TIP}.get(key, '')
        self.pilot_card.set(tone, head, sub, tip)
        if tone == 'ok':
            states = ['done'] * 3
        elif step == 0:
            states = ['todo'] * 3
        else:
            mark = 'fail' if tone == 'error' else 'now'
            states = ['done'] * (step - 1) + [mark] + ['todo'] * (3 - step)
        self.pilot_steps.set_states(states)
        self.pilot_bar.setVisible(busy)
        if busy:
            self.pilot_bar.set_percent(None)
        else:
            self.pilot_bar.timer.stop()
        retry = key in ('failed', 'no_python', 'foreign')
        self.pilot_primary.setText('Find Remote Pilot' if key == 'not_found' else 'Try again' if retry else
                                   'Open Remote Pilot')
        self.pilot_primary.setProperty('kind', 'update' if key == 'not_found' else 'go')
        restyle(self.pilot_primary)
        where = self.pilot_where_kind()
        self.pilot_primary.setToolTip(
            'Shows you where Remote Pilot is, so this app can start it.' if key == 'not_found' else
            'Starts Remote Pilot again and opens it.' if retry else
            'Starts Remote Pilot if it is not running yet and opens it on your Chromatic\'s page' +
            {'window': ', in a window of its own.', 'app': ', in a window of its own (no tabs or address bar).',
             'browser': ', in your web browser.'}[where] + ' There you see the console\'s screen and can press its '
            'buttons.')
        self.pilot_browser.setVisible(found)
        self.pilot_where.setText('' if not found else {
            'window': 'Opens in a window of its own.',
            'app': 'Opens in a window of its own, without tabs or an address bar.',
            'browser': 'Opens in your web browser (this build has no window of its own for it).'}[where])
        self.pilot_where.setToolTip('' if where == 'window' else
                                    'A window inside this app needs an extra part of Qt (Qt WebEngine) that this '
                                    'build does not have, so a browser shows it instead. The page is the same.')
        self.pilot_folder.setVisible(found)
        self.pilot_stop.setVisible(self.pilot.launcher.running())
        if getattr(self, 'current_tab', None) == 'pilot' and not self.busy():
            self.console_art.set_tone(PILOT_ART.get(tone, 'info'))

    def pilot_tab_opened(self):
        """Opening the tab starts Remote Pilot if it is not running (no window opens by itself)."""
        self.refresh_pilot()
        if self.pilot.home() is None:
            return
        self.pilot_timer.start(1000)
        if self._pilot['launch'] == '' and not self.pilot.launcher.started():
            self.pilot_launch(None)
        else:
            self.pilot_poll()

    def _pilot_primary_clicked(self):
        if self._pilot_view == 'not_found':
            self.pilot_find()
            return
        if self._pilot_view in ('failed', 'no_python', 'foreign'):
            self._pilot['launch'] = ''
        self.pilot_launch('window')

    def pilot_launch(self, where):
        """Start Remote Pilot if needed (helper thread), then open the viewer once it answers."""
        if self.pilot.home() is None:
            self.refresh_pilot()
            return
        if where:
            self._pilot['open'] = where
        self.pilot_timer.start(1000)
        if self._pilot['status'] is not None:
            self.pilot_poll()  # it answers already: the poll opens it
            return
        if self._pilot['launch'] == 'starting' or self._pilot['busy']:
            return
        self._pilot.update(launch='starting', at=self.clock(), since=None)
        self.refresh_pilot()
        self._pilot['busy'] = True
        threading.Thread(target=lambda: self.bridge.pilot.emit('started', self.pilot.start()),
                         name='remote-pilot-start', daemon=True).start()

    PILOT_START_S = 30  # no answer this long after starting: it did not start

    def pilot_poll(self):
        """Once a second while the tab is open or a window is to be opened: Remote Pilot's status
        on a helper thread (127.0.0.1 only)."""
        if self._pilot['busy']:
            return
        self._pilot['busy'] = True
        show = bool(self._pilot['open'])

        def fetch():
            try:
                status, foreign = self.pilot.client.status(), False
            except pilot_link.Foreign:
                status, foreign = None, True
            if status is not None and show:
                self.pilot.client.show_chromatic()
            self.bridge.pilot.emit('status', {'status': status, 'foreign': foreign, 'show': show})
        threading.Thread(target=fetch, name='remote-pilot-status', daemon=True).start()

    def on_pilot(self, kind, data):
        """Results of the helper threads, on the window's thread."""
        state = self._pilot
        state['busy'] = False
        if kind == 'started':
            state['launch'] = data or 'starting'
            if data:
                state['open'] = None
                self.add_line({'no_python': 'Remote Pilot could not start: its Python (local\\venv) is missing.',
                               'foreign': 'Remote Pilot could not start: another program uses its port.'}.get(
                    data, 'Remote Pilot could not start.'))
            self.refresh_pilot()
            if not data:
                self.pilot_poll()
            return
        status = data['status']
        if data['foreign'] and state['launch'] != 'starting':
            state['launch'] = 'foreign'
        if status is None and state['launch'] == 'starting':
            exited = self.pilot.launcher.exited()
            if exited is not None or (state['at'] is not None and self.clock() - state['at'] > self.PILOT_START_S):
                state['launch'] = 'failed'
                state['open'] = None
                self.add_line('Remote Pilot did not start. Press Try again.')
        if status is not None:
            if state['launch'] == 'starting':
                self.add_line('Remote Pilot is running.')
            state['launch'] = ''
            transport = status.get('transport') or {}
            connected = transport.get('connected') and transport.get('session')
            state['since'] = None if connected else (state['since'] if state['since'] is not None else self.clock())
        else:
            state['since'] = None
        state['status'] = status
        self.refresh_pilot()
        if status is not None and data['show'] and state['open']:
            where, state['open'] = state['open'], None
            self.pilot_open(where, self.pilot.client.url)
            self.add_line('Remote Pilot opened' + (' in your browser.' if where == 'browser' else '.'))
        if self.current_tab != 'pilot' and not state['open'] and state['launch'] != 'starting':
            self.pilot_timer.stop()

    def open_viewer(self, where, url):
        """Shows the Remote Pilot page: a window of its own when one can be made, else the browser."""
        kind = 'browser' if where == 'browser' else self.pilot_where_kind()
        if kind == 'window':
            try:
                self._pilot_window = PilotWindow(url)
                self._pilot_window.show()
                return
            except Exception:
                kind = 'app' if pilot_link.app_browser() else 'browser'
        if kind == 'app':
            import subprocess
            try:
                subprocess.Popen([str(pilot_link.app_browser()), f'--app={url}', '--window-size=1220,900'],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            except OSError:
                pass
        QDesktopServices.openUrl(QUrl(url))

    def pilot_find(self):
        folder = self.pilot_choose()
        if not folder:
            return
        if not pilot_link.is_home(folder):
            self.pilot_card.set('update', 'That is not Remote Pilot',
                                'Pick the Remote Pilot folder: the one with receiver.py and a chromatic folder in it.')
            self.add_line('That folder is not Remote Pilot. Nothing was changed.')
            return
        try:
            self.service.settings.set(pilot_link.SETTING, str(Path(folder).resolve()))
        except OSError:
            pass
        self.add_line('Remote Pilot found. This app remembers where it is.')
        self._pilot['launch'] = ''
        self.pilot_tab_opened()

    def _choose_pilot_folder(self):
        from PySide6.QtWidgets import QFileDialog
        return QFileDialog.getExistingDirectory(self, 'Where is Remote Pilot?', str(Path.home()))

    def _open_pilot_folder(self):
        home = self.pilot.home()
        if home is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(home.folder)))

    def pilot_stop_clicked(self):
        if self.pilot.stop():
            self.add_line('Remote Pilot closed.')
        self._pilot.update(status=None, launch='', since=None, open=None)
        self.refresh_pilot()

    def show_pilot_sign(self, state, title=''):
        """The USB hand-off sign: while a job holds the cable, and briefly once it is back."""
        if state == 'paused':
            what = (title or 'this job').lower()
            self.pilot_chip.setText('REMOTE PILOT PAUSED')
            self.pilot_chip.setProperty('role', 'notice')
            self.pilot_chip.setToolTip(PILOT_PAUSED_TIP)
            self.work_pilot.setText(f'Remote Pilot paused while it does this ({what}): back in a moment. Its Wi-Fi '
                                    'link keeps working.')
            self.work_pilot.show()
        elif state == 'kept':
            self.pilot_chip.setText('REMOTE PILOT PAUSED')
            self.pilot_chip.setProperty('role', 'notice')
            self.pilot_chip.setToolTip('A job did not finish, so Remote Pilot stays off the USB cable until you press '
                                       'Finish it. Its Wi-Fi link keeps working.')
            self.work_pilot.hide()
        elif state == 'back':
            if self.pilot_chip.isHidden():
                return  # it was never paused (Remote Pilot not running): nothing to say
            self.pilot_chip.setText('REMOTE PILOT BACK')
            self.pilot_chip.setProperty('role', 'ok')
            self.pilot_chip.setToolTip('Remote Pilot has the USB cable back.')
            self.work_pilot.hide()
            QTimer.singleShot(6000, lambda: self.pilot_chip.hide() if self.pilot_chip.text() == 'REMOTE PILOT BACK'
                              else None)
        else:
            return
        restyle(self.pilot_chip)
        self.pilot_chip.show()

    def _guide_page(self):
        """The guided sound setup: step N of 5 with its time, the headline and a few plain lines
        in the speech bubble, the one physical thing to do now (yellow, like the updater's
        notices), its answer buttons and Stop setup. The console drawing shows the step."""
        page, layout = self._page(6)
        top = QHBoxLayout()
        top.setSpacing(8)
        self.guide_strip = StepStrip(SOUND_STEPS)
        top.addWidget(self.guide_strip, 0, Qt.AlignmentFlag.AlignVCenter)
        self.guide_step = label('', 'value', wrap=False, size=theme.GEOMETRY['small_point_size'], bold=True)
        top.addWidget(self.guide_step, 0, Qt.AlignmentFlag.AlignVCenter)
        top.addStretch(1)
        self.guide_time = label('', 'dim', wrap=False, size=theme.GEOMETRY['small_point_size'])
        top.addWidget(self.guide_time, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(top)
        layout.addStretch(1)
        self.guide_card = StatusCard()
        self.guide_card.sub.setProperty('role', 'value')
        self.guide_card.sub.setFont(theme.body_font(theme.GEOMETRY['small_point_size']))
        layout.addWidget(self.guide_card)
        self.guide_todo = label('', 'notice', size=theme.GEOMETRY['small_point_size'] + 0.5, bold=True)
        self.guide_todo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.guide_todo)
        row = self.guide_row = QHBoxLayout()
        row.setSpacing(8)
        row.addStretch(1)
        self.guide_buttons = []
        for n in range(3):
            widget = button('', 'go', size=theme.GEOMETRY['base_point_size'])
            widget.setFixedHeight(38)
            widget.setMinimumWidth(150)
            widget.clicked.connect(lambda _=False, i=n: self.guide_pressed(i))
            row.addWidget(widget, 3)
            self.guide_buttons.append(widget)
        row.addStretch(1)
        layout.addLayout(row)
        layout.addStretch(1)
        self.guide_stop = button('Stop setup', '', size=theme.GEOMETRY['small_point_size'])
        self.guide_stop.setFixedHeight(28)
        self.guide_stop.setToolTip('Ends the setup here. Nothing more runs. A job that is running is never cut '
                                   'off; this button is only on the screens between jobs.')
        self.guide_stop.clicked.connect(lambda: self.guide_choose('stop'))
        layout.addWidget(self.guide_stop, 0, Qt.AlignmentFlag.AlignRight)
        self.guide_choices = [None, None, None]
        return page

    # the guided sound setup -----------------------------------------------------------------
    def sound_image_text(self):
        """'sound build 18.40 (image 2fb37494)' or None; read once (it checks the whole build)."""
        if self._sound_image_text is ...:
            try:
                self._sound_image_text = self.sound_image()
            except Exception:
                self._sound_image_text = None
        return self._sound_image_text

    def link_ready(self):
        """The Home tab offers Add the Pocket link on the sound image when this tool's up-to-date start-up
        menu is there (the one with the Pocket link row) and the reviewed 18.41 image is ready."""
        with self.service._lock:
            summary = {} if self._stale else dict(self.service.summary)
        return (summary.get('state') == 'installed' and not getattr(self, 'pending', False)
                and self.link_image_text() is not None)

    def sound_ready(self):
        """The Home tab offers the sound setup when the console has this tool's up-to-date
        start-up menu (the one with the sound code) and a sound image is built."""
        with self.service._lock:
            summary = {} if self._stale else dict(self.service.summary)
        return (summary.get('state') == 'installed' and not getattr(self, 'pending', False)
                and self.sound_image_text() is not None)

    def link_image_text(self):
        """'sound + Pocket link build 18.41 (image 0123abcd)' when the reviewed image is ready, else None;
        read once (it checks the whole build), again whenever the guide opens."""
        if self._link_image_text is ...:
            try:
                self._link_image_text = self.link_image()
            except Exception:
                self._link_image_text = None
        return self._link_image_text

    def guide_image_text(self):
        link = self.sound is self.link_guide or self.sound is self.link_update
        return self.link_image_text() if link else self.sound_image_text()

    def guide_name(self):
        return self.GUIDE_NAMES.get(getattr(self.sound, 'NAME', ''), 'sound setup')

    def _update_facts(self):
        """The last status summary for the one-step update ({} when none, or when it is stale)."""
        if getattr(self, '_stale', True):
            return {}
        with self.service._lock:
            return dict(self.service.summary)

    def note_guide(self):
        """Tell the service which one-step update screen shows (fixed wording), for the API."""
        note = getattr(self.service, 'note_guide', None)
        if note is not None:
            view = getattr(self.sound, 'public_view', None)
            note(view() if view is not None and self.sound.active else None)

    def _quiet_jobs(self):
        """The console chip jobs object, for the two answers-only calls (no device is opened)."""
        from types import SimpleNamespace
        return self.service.fpga_factory(SimpleNamespace(say=self.add_line, stop_point=lambda: None), None)

    def _confirm_link_try(self, answers):
        """The owner's answers after the four looks, recorded with the try (FpgaJobs.confirm_link_try)."""
        good = self._quiet_jobs().confirm_link_try(answers)
        self.add_line('Your answers were saved with the try: ' + (
            'every check All good.' if good else
            'they do not confirm it (the line above says why), so it is not written.' if link_answers_good(answers)
            else 'something was not right, so it is not written.'))
        return good

    def _link_try_confirmed(self):
        try:
            jobs = self._quiet_jobs()
            return jobs.link_try_confirmed(jobs.link_loader().fingerprint) is not None
        except Exception:
            return False

    def open_link_guide(self):
        """Add the Pocket link: the guide's first screen (or, when the reviewed image is not there,
        a screen that says so). Nothing runs until a button there (and that job's own Approve)."""
        if self.busy() or self.service.busy() or self._confirming:
            return False
        self._link_image_text = ...  # read the build again: it may have been cleared or rebuilt
        self.sound = self.link_guide
        self.link_guide.start(ready=self.link_image_text() is not None)
        self.add_line('Pocket link setup started (5 steps, about 15 minutes). Every write still asks for your Approve.'
                      if self.link_guide.screen == 'check' else
                      'The Pocket link image is not cleared for your console yet. Nothing was done.')
        self.show_guide()
        return True

    def open_link_update(self, asked_by=None):
        """Update the Pocket link in one step: its first screen (or, when the reviewed image is not
        there, a screen that says so). Nothing runs until Update now and that job's own Approve.
        `asked_by`: the program that asked through the API (the screen names it)."""
        if self.busy() or self.service.busy() or self._confirming:
            return False
        self._link_image_text = ...  # read the build again: it may have been cleared or rebuilt
        self.sound = self.link_update
        self.link_update.start(ready=self.link_image_text() is not None, asked_by=asked_by)
        self.add_line('Update the Pocket link opened: one step, no questions. Every write still asks for your Approve.'
                      if self.link_update.screen == 'start' else
                      'The Pocket link image is not cleared for your console yet. Nothing was done.')
        self.show_guide()
        return True

    def open_sound_guide(self, update=False):
        """Set up Jellyfin sound (or, with `update`, Update sound: the same five steps and jobs over
        an older sound image): the guide's first screen. Nothing runs until a button there (and that
        job's own Approve)."""
        if self.busy() or self.service.busy() or self._confirming:
            return False
        self._sound_image_text = ...  # read the build again: it may have been rebuilt
        self.sound = self.sound_guide
        self.sound.start(update=bool(update))
        self.add_line(('Sound update' if update else 'Sound setup') + ' started (5 steps, about 15 minutes). Every '
                      'write still asks for your Approve.')
        self.show_guide()
        return True

    def show_guide(self):
        """Draw the guide's current screen into the bubble (or leave it when it has closed)."""
        if not self.sound.active:
            return
        guide = self.sound
        view = guide.view(self.guide_image_text() if guide.screen in guide.IMAGE_SCREENS else None)
        self.title.setText(guide.TITLE)
        self.guide_strip.set_step(view.step)
        self.guide_strip.setVisible(bool(guide.STEPS) and (view.step > 0 or view.key in ('done', 'restored',
                                                                                         'sound_back')))
        if view.step:
            left = guide.minutes_left(view.step)
            self.guide_step.setText(f'STEP {view.step} OF {guide.STEPS}')
            self.guide_time.setText(f'About {guide.MINUTES[view.step]} min  |  {left} min left')
        else:
            self.guide_step.setText(guide.TAG)
            self.guide_time.setText('')
        self.guide_card.set(view.tone if view.tone in TONE_INK else 'info', view.headline, '\n'.join(view.lines),
                            view.detail)
        self.guide_todo.setText(view.todo)
        self.guide_todo.setVisible(bool(view.todo))
        buttons = [b for b in view.buttons if b[2] != 'stop']
        self.guide_choices = [None] * len(self.guide_buttons)
        for n, widget in enumerate(self.guide_buttons):
            if n < len(buttons):
                text, kind, choice = buttons[n]
                widget.setText(text)
                widget.setProperty('kind', kind or 'no')
                widget.setToolTip(view.detail if n == 0 else '')
                widget.setMinimumWidth(150 if len(buttons) < 3 else 96)
                self.guide_choices[n] = choice
                widget.show()
                widget.update()
            else:
                widget.hide()
        # One answer: a centred button. Two or three: they share the bubble's width, so long answers
        # ("Game looks and sounds normal") stay readable.
        edge = 1 if len(buttons) < 2 else 0
        self.guide_row.setStretch(0, edge)
        self.guide_row.setStretch(len(self.guide_buttons) + 1, edge)
        self.guide_stop.setVisible(any(b[2] == 'stop' for b in view.buttons))
        busy = self.busy() or self.service.busy()
        for widget in self.guide_buttons + [self.guide_stop]:
            widget.setEnabled(not busy)
        for tab in self.tabs.values():
            tab.setEnabled(False)  # one thing at a time: the guide's own buttons, or Stop setup
        self.stack.setCurrentWidget(self.pages['guide'])
        self.console_art.set_tone(view.tone)
        self.note_guide()
        self.update()

    def guide_pressed(self, n):
        choice = self.guide_choices[n] if n < len(self.guide_choices) else None
        if choice:
            self.guide_choose(choice)

    def guide_choose(self, choice):
        """A button on a guide screen. A job starts only through start(), so through that job's
        own Approve dialog; Cancel there leaves the screen as it was. The one exception is the
        one-step update's Update now: that press writes nothing, and the job asks for its one
        Approve itself, later, with the new image running (Dialogs.ask), so it shows no dialog here."""
        if self.busy() or self.service.busy() or self._confirming or not self.sound.active:
            return
        if choice == 'stop':
            name = self.guide_name()
            self.add_line(f'{name[0].upper()}{name[1:]} stopped by you. Nothing more will run.')
        outcome = self.sound.choose(choice)
        if outcome and outcome[0] == 'job':
            key = outcome[1]
            self.sound.job = key
            self._guide_job = key
            if self.sound is self.link_update and key in OWNER_APPROVE_KEYS:
                started = self.start(key, confirmed=True, confirmed_by=STARTED_IN_WINDOW)
            else:
                started = self.start(key)
            if not started:
                self._guide_job = None
                self.sound.cancelled(key)
                self.show_guide()
            return
        if outcome and outcome[0] in ('close', 'tab'):
            self.leave_guide(outcome[1] if outcome[0] == 'tab' else None)
            return
        if outcome and outcome[0] == 'guide':  # Set up sound first, or the Pocket link step by step
            self.leave_guide()
            if len(outcome) > 1 and outcome[1] == 'link':
                self.open_link_guide()
            else:
                self.open_sound_guide()
            return
        self.show_guide()

    def leave_guide(self, tab=None):
        self.note_guide()
        for t in self.tabs.values():
            t.setEnabled(True)
        self.refresh_guide()
        self.show_tab(tab or getattr(self, 'current_tab', 'console'))

    def guide_after_job(self, key, code, result):
        """The guide's job has ended: its next screen, from what the job said."""
        self._guide_job = None
        self.sound.job_done(key, code, list(self.run_lines), result.get('headline', ''), result.get('what_to_do', ''))
        self.show_guide()
        follow = getattr(self.sound, 'auto', None)
        if follow:  # the one-step update goes on by itself: the next job, through its own Approve dialog
            QTimer.singleShot(0, lambda: self.guide_follow(follow))

    def guide_follow(self, key):
        """Start the job the one-step update named as next. It goes through guide_choose and start(),
        so a write still shows its Approve dialog; Cancel there leaves the screen with its button.
        Not while a missed owner action's red sign is in front: its OK comes first."""
        if (not self.sound.active or getattr(self.sound, 'auto', None) != key or self._owner is not None
                or self.busy() or self.service.busy() or self._confirming):
            return
        self.guide_choose(f'job:{key}')

    def _primary_clicked(self):
        if getattr(self, '_primary_sound', None) == 'link':
            self.open_link_update()
        elif getattr(self, '_primary_sound', None):
            self.open_sound_guide(update=self._primary_sound == 'update')
        elif self.step_now.key:
            self.start(self.step_now.key)

    def _result_sound_clicked(self):
        """After the guided official update: the sound setup again (its own five steps, each job
        with its own Approve)."""
        self.result_sound.hide()
        self.result_ok.setText('OK')
        self.show_tab('console')
        return self.open_sound_guide()

    def pc_jellyfin(self):
        """Does this PC have its private Jellyfin settings? (Whether the file exists, nothing more.)"""
        try:
            return bool(self.cm.jellyfin_settings_available())
        except Exception:
            return False

    def refresh_setup_line(self, summary=None, step=None):
        """The Home tab's Wi-Fi and Jellyfin line (core.setup_sign), shown under the status when
        the console has the start-up menu and the main status is not something more urgent."""
        if summary is None:
            with self.service._lock:
                summary = {} if self._stale else dict(self.service.summary)
        step = step or self.step_now
        sign = setup_sign(summary, self.pc_jellyfin())
        urgent = step is not None and step.key not in (None, 'install')
        self.setup_now = None if (sign is None or urgent or self.consoles == 0) else sign
        self.setup_row.setVisible(self.setup_now is not None)
        if self.setup_now is None:
            return
        self.setup_mark.set_tone(sign.tone)
        self.setup_text.setText(sign.text)
        self.setup_text.setProperty('role', {'ok': 'ok', 'update': 'update', 'error': 'error'}.get(sign.tone, 'value'))
        restyle(self.setup_text)
        self.setup_row.setToolTip(sign.detail)
        self.setup_text.setToolTip(sign.detail)
        self.setup_button.setVisible(bool(sign.key))
        self.setup_button.setText(sign.button)
        self.setup_button.setProperty('kind', 'error' if sign.tone == 'error' else 'go')
        self.setup_button.setToolTip(sign.detail)
        self.setup_button.setEnabled(not self.busy())
        self.setup_button.update()

    def _setup_clicked(self):
        sign = self.setup_now
        if sign is None or not sign.key:
            return
        if sign.key == JELLYFIN_PAGE:
            self.open_jellyfin_page()
        else:
            self.start(sign.key)

    def open_jellyfin_page(self):
        """Settings > Jellyfin server (JellyfinDialog). New Jellyfin sign-in from there starts the
        normal job, with its own Approve. Returns the dialog's choice ('relink' or None)."""
        if self.busy() or self.service.busy() or self._confirming:
            return None
        self._confirming = True
        try:
            dialog = JellyfinDialog(self, **self.jellyfin_options)
            self.jellyfin_dialog = dialog
            dialog.exec()
        finally:
            self._confirming = False
            self.jellyfin_dialog = None
        self.refresh_jellyfin_state()
        self.refresh_guide()
        if dialog.choice == 'relink':
            self.start('jellyfin')
        return dialog.choice

    def refresh_jellyfin_state(self):
        if not hasattr(self, 'jellyfin_state'):
            return
        ready = self.pc_jellyfin()
        self.jellyfin_mark.set_tone('ok' if ready else 'update')
        self.jellyfin_state.setText('Saved on this PC.' if ready else
                                    'Not set up yet. The console\'s Jellyfin sign-in needs it.')
        self.jellyfin_state.setProperty('role', 'ok' if ready else 'update')
        restyle(self.jellyfin_state)

    PRIMARY_KINDS = {'update': 'update', 'error': 'error', 'ok': 'ok', 'info': 'go'}

    def refresh_guide(self):
        """The Home tab's one status and next step, from the last status, an unfinished job
        and how many consoles are plugged in (when this window can tell). With nothing to
        press, there is no button at all (never a greyed-out one)."""
        with self.service._lock:
            summary = {} if self._stale else dict(self.service.summary)
        self.step_now = step = guide(summary, getattr(self, 'pending', False), self.consoles)
        self.status_card.set(step.tone, step.headline, '\n'.join(step.steps), step.detail)
        self.primary.setText(step.button)
        self.primary.setToolTip(step.detail)
        self.primary.setProperty('kind', self.PRIMARY_KINDS.get(step.tone, 'go'))
        self.primary.update()
        # Up to date: what the console chip runs, as the start-up menu reported it at the last check,
        # decides the sound button: none for the sound image (status only), Update sound for an older
        # one, the guided setup for ModRetro's stock image (with a built sound image), none otherwise.
        sound = sound_state(summary, built_sound_version(self.sound_image_text())) if (
            step.key is None and step.tone == 'ok') else 'n/a'
        self._primary_sound = None
        if sound == 'none' and self.sound_ready():
            self._primary_sound = 'setup'
            self.primary.setText(self.SOUND_BUTTON)
            self.primary.setToolTip(SOUND_TIP)
            self.primary.setProperty('kind', 'go')
        elif sound == 'outdated' and self.sound_ready():
            self._primary_sound = 'update'
            self.primary.setText(self.UPDATE_SOUND_BUTTON)
            self.primary.setToolTip(UPDATE_SOUND_TIP)
            self.primary.setProperty('kind', 'update')
        link = link_state(summary) if sound != 'n/a' else None
        if sound == 'installed' and link is None and self.link_ready():
            self._primary_sound = 'link'
            self.primary.setText(self.LINK_BUTTON)
            self.primary.setToolTip(LINK_UPDATE_TIP)
            self.primary.setProperty('kind', 'go')
        line = sound_line(sound, summary.get('fpga'), self.sound_image_text(), link)
        self.sound_line.setVisible(line is not None)
        if line:
            self.sound_line.setText(line[0])
            self.sound_line.setProperty('role', line[1])
            self.sound_line.setToolTip(line[2])
            restyle(self.sound_line)
        self.refresh_setup_line(summary, step)
        self.primary.setVisible(step.key is not None or bool(self._primary_sound))
        self.primary.setEnabled((step.key is not None or bool(self._primary_sound)) and not self.busy())
        self.change_setup.setVisible(step.key == 'install')
        self._home_tone = 'sound' if sound == 'installed' else 'update' if sound == 'outdated' else step.tone
        if getattr(self, 'current_tab', 'console') == 'console' and not self.busy():
            self.console_art.set_tone(self._home_tone)

    PLUG_ABSENT_S = 10   # the console must have been gone this long for a plug-in to count
    AFTER_JOB_S = 90     # no check by itself this soon after any job (or reset) of this tool

    def poll_console(self):
        """Every second and a half: how many consoles are plugged in (USB enumeration only;
        the console itself is not touched). The console also drops off USB and comes back by
        itself: when stock starts after the menu (its player number changes), during link
        play, and while ModRetro's updater reboots it. Taking the port then would reset the
        chip mid-update or mid-game. So the read-only check runs by itself only for a real
        plug-in: after the console was absent for PLUG_ABSENT_S continuously, never within
        AFTER_JOB_S of a job of this tool, never while ModRetro's updater runs, and at most
        once per plug-in. Otherwise the Home tab shows Check now (one click)."""
        try:
            count = int(self.detect())
        except Exception:
            count = None
        now = self.clock()
        if count == 0:
            if self._absent_since is None:
                self._absent_since = now
            elif now - self._absent_since >= self.PLUG_ABSENT_S:
                self._plug_armed = True  # really unplugged: the next appearance is a plug-in
        elif count is not None:
            self._absent_since = None
        appeared = count == 1 and self.consoles != 1
        if count != self.consoles:
            self.consoles = count
            self.refresh_guide()
        if not appeared or not self._plug_armed:
            return
        self._plug_armed = False  # at most once per plug-in, whatever happens next
        self._stale = True        # it was away: what the panel says may no longer be true
        self.refresh_guide()
        if self.sound.active:  # the sound setup asks the owner to switch off and on: never check by itself then
            self.add_line('Chromatic found. The sound setup goes on when you press its button.')
            return
        if (self.busy() or self.service.busy() or self._confirming
                or now < self._quiet_until or self.updater_running()):
            self.add_line('Chromatic found. Press Check now when it is ready.')
            return
        self.add_line('Chromatic found. Checking it now (about 20 seconds; nothing is changed).')
        self.start('status')

    # behaviour --------------------------------------------------------------------------
    def show_tab(self, key):
        if self.busy() and key != getattr(self, 'current_tab', None):
            self.tabs[key].setChecked(False)
            return
        self.current_tab = key
        if not self.busy():
            self._owner, self._owner_after = None, None  # a "Time ran out" sign left by a tab is closed
        for name, tab in self.tabs.items():
            tab.setChecked(name == key)
        if key == 'settings':
            self.refresh_ai()
            self.refresh_jellyfin_state()
        self.title.setText(self.TITLES[key])
        if key == 'pilot':
            self.pilot_tab_opened()
        elif not self._pilot['open']:
            self.pilot_timer.stop()
        self.stack.setCurrentWidget(self.pages[key])
        self.console_art.set_tone(getattr(self, '_home_tone', self.status_card.tone) if key == 'console' else
                                  PILOT_ART.get(self.pilot_card.tone, 'info') if key == 'pilot' else
                                  'error' if key == 'recovery' and self.status_card.tone == 'error' else 'info')
        self.update()  # the border and band take the tab's colour

    def busy(self):
        return self.running is not None

    # The window's view of the shared service (tests and --auto read these).
    @property
    def manager_factory(self):
        return self.service.manager_factory

    @manager_factory.setter
    def manager_factory(self, factory):
        self.service.manager_factory = factory

    @property
    def tracker(self):
        return self.service.tracker

    @property
    def progress(self):
        return self.service.progress

    @property
    def run_log(self):
        return self.service.run_log

    def refresh_pending(self):
        pending = self.service.pending()
        self.pending = pending
        for action in ACTIONS:
            widget = self.buttons.get(action.key)
            if widget is not None:
                widget.setEnabled(not self.busy() and (action.pending_ok or not pending)
                                  and (action.key not in ('resume', 'abandon') or pending))
        self.pending_note.setText('A job did not finish. Press Finish it on the Home tab.' if pending else '')
        if hasattr(self, 'primary'):
            self.refresh_guide()

    # AI control ------------------------------------------------------------------------
    def refresh_ai(self):
        """The AI chip, Stop buttons and the Settings tab, from the service's AI state."""
        if not hasattr(self, 'ai_remembered'):
            return
        decision, remembered = self.service.ai_decision(), self.service.remembered_ai_control()
        allowed = decision == 'allow'
        self.ai_chip.setVisible(allowed)
        self.ai_stop.setVisible(allowed)
        self.ai_stop_settings.setVisible(allowed)
        self.ai_remembered.setText('Remembered choice: ' + {
            'allow': 'allow (you confirm it once each time the app starts)',
            'deny': 'never allow (you approve each change)'}.get(remembered, 'none (it asks you the first time)'))
        self.ai_now.setText('Right now: ' + {'allow': 'the AI may use this app',
                                             'deny': 'you approve each change'}.get(decision,
                                                                                    'not asked yet'))
        self.ai_forget.setEnabled(remembered is not None)
        self.work_stop.setVisible(self.busy() and self.service.ai_job())

    def peek_release(self):
        """(fingerprint, '1.2.0 (image 48b11f9a)') of what an install would write now, or None.
        Reads without re-pinning anything."""
        try:
            release = self.service.release_peek()
            return release.fingerprint(), short_release(release)
        except Exception:
            return None

    def forget_ai(self):
        self.service.forget_ai_control()
        self.add_line('AI choice forgotten. The next time an AI asks, you are asked again.')
        self.refresh_ai()

    def stop_ai(self):
        """The owner's Stop: the AI loses its permission at once (see ManagerService.stop_ai)."""
        outcome = self.service.stop_ai()
        if outcome is not None and not outcome['stopping_job']:
            self.add_line('AI stopped. It now needs your Approve for each change.')
        self.refresh_ai()
        if outcome and outcome['stopping_job']:
            self.work_stop.setEnabled(False)
            self.work_stop.setText('Stopping...')
        return outcome

    def start(self, key, confirmed=False, confirmed_by=None):
        """A button: writes need the person's approval (the Approve dialog) first. Install
        carries the Wi-Fi/Jellyfin choice (the Change tick box) into the same job. `confirmed`
        with `confirmed_by` is only for a job that asks for its Approve itself (guide_choose)."""
        if self.busy() or self.service.busy() or self._confirming:
            return False
        action = BY_KEY[key]
        if action.confirm and not confirmed:
            self._confirming = True
            try:
                ok = self.dialogs.confirm(action)
            finally:
                self._confirming = False
            if not ok:
                self.add_line(f'{action.title}: cancelled. Nothing done.')
                return False
        # The service announces 'started' straight back on this thread, which sets up the screen.
        setup = 'change' if key == 'install' and self.change_setup.isChecked() else 'auto'
        started = self.service.start(key, requested_by=OWNER, setup=setup,
                                     confirmed_by=confirmed_by or (APPROVED_IN_WINDOW if action.confirm else None))
        if started:
            self.change_setup.setChecked(False)  # a one-time choice
        return started

    def on_service(self, event, data):
        """Every event from the shared service, on the window's thread."""
        if event == 'started':
            self.on_started(data['key'], data['requested_by'])
        elif event == 'line':
            self.add_line(data['text'])
        elif event == 'device':
            if data['kind'] == 'mode':
                self.show_banner()
            self.refresh_work()
        elif event == 'request':
            self.on_request(data)
        elif event == 'ui':
            self.set_log_open(data.get('log_panel_open', False))
        elif event == 'pilot':
            self.show_pilot_sign(data.get('state'), data.get('title', ''))
        elif event == 'owner':
            self.on_owner(data)
        elif event == 'done':
            self.on_done(data['code'])

    def on_started(self, key, requested_by):
        action = BY_KEY[key]
        self.running = key
        with self.service._lock:
            self._summary_before = dict(self.service.summary)  # the guided update compares the sound image with it
        self._quiet_until = self.clock() + self.AFTER_JOB_S
        self.run_lines = []
        self._owner, self._owner_after = None, None
        self.owner_timer.stop()
        by_ai = requested_by != OWNER
        self.add_line(f'--- {action.title} ---' + ('' if not by_ai else f' (asked by {requested_by})'))
        self.work_title.setText(action.title if not by_ai else f'{program_name(requested_by)}: {action.title}')
        self.work_card.set('busy', self.work_title.text(), '')
        self.work_phase.setText('Starting')
        self.work_step.setText('')
        self.work_line.setText('')
        self.work_stop.setEnabled(True)
        self.work_stop.setText('Stop the AI')
        note = (self.service.job or {}).get('note')
        self.work_usage.setText(note or '')
        self.work_usage.setVisible(bool(note))
        self.work_pilot.hide()
        self.show_banner()
        self.bar.set_percent(None)
        if not self.isActiveWindow() and by_ai:
            self.raise_()
        self.stack.setCurrentWidget(self.pages['working'])
        self.console_art.set_tone('busy')
        self.refresh_pending()
        for tab in self.tabs.values():
            tab.setEnabled(False)
        self.refresh_ai()
        self.clock_timer.start(250)
        self.refresh_work()

    def on_request(self, request):
        """A program asked, through the local API, for a write (or the status). The first
        request of a run asks the owner whether the AI may use the app at all; allowed, it
        starts after the hardware usage rule; otherwise it is put to the person with the
        same Approve dialog as the button, and only their click on Approve starts it."""
        action = BY_KEY[request['action']]
        current = self.service.request
        if current is None or current['id'] != request['id'] or current['state'] != 'waiting':
            return  # withdrawn (or replaced) before the window got to it
        if self.busy() or self.service.busy() or self._confirming:
            self.service.resolve_request(request['id'], False, 'The window was busy; ask again later.')
            return
        if request['action'] in OWNER_APPROVE_KEYS and (not self.sound.active or self.sound is self.link_update):
            return self.on_update_request(request)
        if self.sound.active:  # the owner is in a guided setup (sound or Pocket link): nothing else starts meanwhile
            guide = self.guide_name()
            self.add_line(f'{program_name(request["requested_by"])} asked to: {action.title}. Not started: the '
                          f'{guide} is open.')
            self.service.resolve_request(request['id'], False, f'The owner is using the guided {guide}; ask again '
                                                               'when it has finished.')
            return
        who = request['requested_by']
        decision = self.service.ai_decision()
        image = self.peek_release()
        if decision is None or (decision == 'allow' and request['action'] in WRITE_KEYS
                                and (image or (None,))[0] != self.service.ai_release):
            # Authority only ever comes from a click here, in this run: a first-time pop-up, or,
            # with Allow remembered (a preference in a file anyone could edit) or a changed
            # start-up menu image, "Keep allowing it this time?".
            self.showNormal()
            self.raise_()
            QApplication.alert(self)
            self._confirming = True
            changed = decision == 'allow'
            try:
                if changed or self.service.remembered_ai_control() == 'allow':
                    allow, remember = self.dialogs.ai_keep(who, action, image and image[1], changed), False
                else:
                    allow, remember = self.dialogs.ai_control(who, action, image and image[1])
            finally:
                self._confirming = False
            self.service.set_ai_control(allow, remember, image and image[0])
            decision = 'allow' if allow else 'deny'
            self.add_line(f'AI control: {"allowed" if allow else "not allowed"}'
                          + (' (choice remembered).' if remember else ' (until this app closes).'))
            self.refresh_ai()
            current = self.service.request
            if current is None or current['id'] != request['id'] or current['state'] != 'waiting':
                return
        if decision == 'allow':
            why = self.service.ai_gate(request['action'])
            usage = None
            if not why and request['action'] in WRITE_KEYS:
                usage = self.service.usage_note()
                if not usage:
                    why = 'No usage reading from the AI was found. Refresh it, then ask again.'
            if why:
                self.add_line(f'{program_name(who)} asked to: {action.title}. Not started: {why}')
                self.service.resolve_request(request['id'], False, why)
                return
            self.add_line(f'{program_name(who)} is starting: {action.title} (you allowed AI control; press Stop AI '
                          'to stop it).')
            self.service.resolve_request(request['id'], True, by_grant=True, usage=usage)
            return
        self.add_line(f'{who} asks to: {action.title}. Waiting for you to approve or cancel it here.')
        self.showNormal()
        self.raise_()  # in front, but no keyboard focus taken here; the dialog ignores input for a second
        QApplication.alert(self)
        self._confirming = True
        try:
            ok = self.dialogs.confirm(action, requested_by=who, reason=request.get('reason', ''))
        finally:
            self._confirming = False
        if not ok:
            self.add_line(f'Request from {who} cancelled. Nothing done.')
        self.service.resolve_request(request['id'], ok, '' if ok else 'The owner did not approve it.')

    def on_update_request(self, request):
        """A program asked for the one-step Pocket link update. The window opens that screen and
        shows an Approve dialog naming who asked. Only the owner's click on Approve starts it (the
        try from RAM; nothing is written): the AI permission is never consulted. The job then asks
        for its own Approve with the new image running, like a press of Update now, and the later
        steps (the start-up menu, with its own Approve) run in the window the same way."""
        key, who = request['action'], request['requested_by']
        action = BY_KEY[key]
        self.add_line(f'{who} asks to: {action.title}. Waiting for you to approve or cancel it here.')
        self.showNormal()
        self.raise_()
        QApplication.alert(self)
        if not self.open_link_update(asked_by=program_name(who)):
            self.service.resolve_request(request['id'], False, 'The window was busy; ask again later.')
            return
        outcome = self.link_update.choose('run') if self.link_update.screen == 'start' else None
        self.show_guide()
        if not outcome:  # no reviewed image, or no start-up menu: the screen says which
            view = self.link_update.view()
            self.add_line(f'Request from {who} not started: {view.headline}.')
            self.service.resolve_request(request['id'], False, f'Not started: {view.headline}. {view.todo}')
            return
        self._confirming = True
        try:
            ok = self.dialogs.confirm(action, requested_by=who, reason=request.get('reason', ''))
        finally:
            self._confirming = False
        if ok:
            self.link_update.job = key
            self._guide_job = key
            if self.service.resolve_request(request['id'], True):
                return
            self._guide_job = None
        else:
            self.add_line(f'Request from {who} cancelled. Nothing done.')
            self.service.resolve_request(request['id'], False, 'The owner did not approve it.')
        self.link_update.cancelled(key)
        self.show_guide()

    def add_line(self, text):
        self.lines.append(text)
        self.run_lines.append(text)
        self.log_line(text)
        if self.busy():
            self.work_phase.setText(self.progress.phase)
            if text.strip():
                self.work_line.setText(text.strip())
            self.refresh_work()

    def show_banner(self):
        banner = BANNERS.get(self.tracker.mode)
        if banner:
            self.banner_head.setText(banner[0])
            self.banner_text.setText(banner[1])
            self.banner.setProperty('mode', self.tracker.mode)
            restyle(self.banner)
        self.banner.setVisible(bool(banner) and self.busy())

    def refresh_work(self):
        """Runs every quarter second on the window's thread while a job runs."""
        if not self.busy():
            return
        with self.service._lock:
            view = self.tracker.view(self.progress.percent)
            phase, mode, asking = self.progress.phase, self.tracker.mode, self.service.question is not None
        todo = physical_step(phase, mode, asking)
        if self.work_todo.text() != todo:
            self.work_todo.setText(todo)
            self.work_todo.setVisible(True)
        self.work_step.setText(view['name'])
        self.work_time.setText(view['detail'])
        self.work_percent.setText('' if view['percent'] is None else f'{view["percent"]}%')
        self.bar.set_percent(view['percent'])

    def ask_from_job(self, prompt, secret):
        """The service asks on the job's thread: the window answers on its own thread."""
        self._answered.clear()
        self.bridge.question.emit(str(prompt), secret)
        self._answered.wait()
        return self._answer

    def on_question(self, prompt, secret):
        self._answer = ''
        try:
            self._answer = self.dialogs.ask(prompt, secret)
        finally:
            self._answered.set()  # a dialog that fails gives an empty answer (no), never a stuck job

    def on_done(self, code):
        result = self.service.result()
        key = self.running
        self.running = None
        self._quiet_until = self.clock() + self.AFTER_JOB_S  # it reset the console: let it settle
        self.clock_timer.stop()
        self.last_code = code
        found = summarize(self.run_lines) if key == 'status' and code == self.cm.EXIT_OK else {}
        if found:
            self._stale = False
            self.show_summary(found)
        elif key != 'status' and key in WRITE_KEYS:
            self.after_write(key, code)
        head, self.next_text, took = result['headline'], result['next'], result['took']
        tone = RESULT_TONES.get(result['role'], 'update')
        if 'download mode' in self.next_text or 'damaged' in self.next_text:
            tone = 'error' if tone != 'ok' else 'update'
        todo = result.get('what_to_do') or 'Nothing else is needed.'
        # A check that finds work to do must say so (a green "nothing else is
        # needed" while the details said an update was waiting).
        if key == 'status' and found.get('state') == 'outdated':
            tone, head = 'update', 'Update available'
            todo = (f'Your start-up menu is {found.get("launcher", "older")}; {found.get("release", "a new one")} '
                    f'is ready. Press OK, then Update to {found.get("release", "the new version")}.')
        elif key == 'status' and found.get('state') == 'stock':
            tone, head = 'update', 'No start-up menu yet'
            todo = 'Press OK, then Install to add the start-up menu.'
        offer = None
        if key == 'update' and code == self.cm.EXIT_OK:
            offer = sound_after_update(self.run_lines, self._summary_before,
                                       built_sound_version(self.sound_image_text()))
            if offer:
                todo = offer[0] + ('' if not offer[1] or self.sound_image_text() else
                                   ' No sound image is built on this PC yet.')
                self.add_line(f'Last step: {offer[0]}')
        sound_offer = bool(offer and offer[1] and self.sound_image_text())
        self.result_sound.setVisible(sound_offer)
        self.result_ok.setText('Not now' if sound_offer else 'OK')
        self.result_card.set(tone, head, self.next_text)
        self.result_todo.setText(f'What to do now: {todo}')
        self.result_fix.setVisible(tone == 'error')
        self.result_ok.setProperty('kind', 'no' if sound_offer else 'ok' if tone == 'ok' else 'go')
        restyle(self.result_ok)
        shown = [line for line in self.run_lines if line.strip() and not line.startswith('---')][-10:]
        shown.append(f'Took {took}.' + (f' Log: {result["log_file"]}' if result['log_file'] else ''))
        self.result_text.setText('\n'.join(shown))
        self.log_line(f'{head}. {self.next_text} (took {took})')
        self.banner.hide()
        self.work_stop.hide()
        for tab in self.tabs.values():
            tab.setEnabled(True)
        self.bar.set_percent(100 if code in (0, 2) else 0, moving=False)
        self.work_percent.setText('')
        self.refresh_pending()
        self.refresh_ai()
        self.stack.setCurrentWidget(self.pages['result'])
        self.console_art.set_tone(tone)
        if self._guide_job is not None and self._guide_job == key and self.sound.active:
            self.guide_after_job(key, code, result)  # the sound setup's next screen instead of the result card
        self.owner_timer.stop()
        if self._owner is not None and self._owner['state'] == owner_actions.TIMEOUT:
            # The owner missed a timed request: the red sign with the next step stays in front until
            # OK, then the screen the job ended on (its result, or the guide's next screen) shows.
            self._owner_after = (self.stack.currentWidget(), self.console_art.tone)
            self.stack.setCurrentWidget(self.pages['owner'])
            self.console_art.set_tone('error')
        else:
            self._owner = None
        self.finished.emit(code)

    def after_write(self, key, code):
        """The side facts showed the console before this job. Never leave them stale: the
        service's summary_after_job (shared with the API) says what is known now: a verified
        install from its own whole-flash check, a job that wrote nothing leaves it as it was,
        anything else asks for a fresh check."""
        with self.service._lock:
            summary = dict(self.service.summary)
        if summary:
            self.show_summary(summary)

    def show_summary(self, found):
        state = found.get('state')
        if state == 'changed':
            self.summary_state.setText(CHANGED_TEXT)
            self.summary_boots.setText(found.get('boots', '--'))
            self.summary_menu.setText('press Check now to see it again')
            self.summary_setup.setText('press Check now to see it again')
            return
        launcher, release = found.get('launcher'), found.get('release')
        if state:
            current = state == 'installed' and launcher is not None and launcher == release
            self.summary_state.setText(UP_TO_DATE if current else STATE_NAMES.get(state, state))
            self.summary_boots.setText(found.get('boots', '--'))
        if 'stock' in found:
            self.summary_stock.setText(f'{found["stock"]} (ESP-IDF {found["idf"]})')
        if launcher and found.get('by'):
            self.summary_menu.setText(f'{launcher} (checked by this install)')
        elif launcher and launcher == release:
            self.summary_menu.setText(f'{launcher} (up to date)')
        elif launcher:
            self.summary_menu.setText(f'{launcher}; {release} is ready to install')
        elif state:
            self.summary_menu.setText('not installed')
        if state:
            self.summary_setup.setText(found.get('setup', 'none (no start-up menu)'))

    def show_licences(self):
        folder = licence_folder(self.root)
        text = ('This program uses Qt for Python (PySide6, LGPL-3.0) and, in the portable build, PyInstaller '
                '(GPL-2.0 with its bootloader exception), esptool (GPL-2.0-or-later), pyserial (BSD) and other '
                'open-source packages. Their licences and notices are in:\n' + str(folder))
        self.dialogs.info('Licences', text)
        if folder.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def closeEvent(self, event):
        if self.busy():
            self.dialogs.info(APP_NAME, 'A job is running. Closing now could interrupt a change; wait for it to '
                                        'finish.')
            event.ignore()
            return
        self.pilot_timer.stop()
        if self.pilot.stop():  # only a Remote Pilot this window started; one started elsewhere keeps running
            self.log_line('Remote Pilot closed (Chromatic Manager had started it).')
        event.accept()

    # A frameless window moves by its top strip (beside the tabs) and by the whole header band.
    def band_bottom(self):
        """The header band's lower edge in body coordinates."""
        return self.title.geometry().bottom() + 6

    def begin_move(self, global_pos):
        """The system's own move (smooth, with Windows snap) when available, else a manual drag."""
        handle = self.windowHandle()
        if handle is not None and handle.startSystemMove():
            self._drag = None
            return
        self._drag = global_pos - self.frameGeometry().topLeft()

    def eventFilter(self, obj, event):
        if self.frameless and obj in (self.title, self.body):
            kind = event.type()
            if kind == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton and (
                    obj is self.title or event.position().y() <= self.band_bottom()):
                self.begin_move(event.globalPosition().toPoint())
                return True
            if kind == QEvent.Type.MouseMove and self._drag is not None and event.buttons() & Qt.MouseButton.LeftButton:
                self.move(event.globalPosition().toPoint() - self._drag)
                return True
            if kind == QEvent.Type.MouseButtonRelease and self._drag is not None:
                self._drag = None
                return True
        return super().eventFilter(obj, event)

    RESIZE_MARGIN = 8  # pixels at each edge that resize the window, like a normal window frame

    def resize_edges(self, pos):
        """Which window edges a point (window coordinates) is on, as Qt.Edge flags (0 if none)."""
        m, edges = self.RESIZE_MARGIN, Qt.Edge(0)
        if pos.x() <= m:
            edges |= Qt.Edge.LeftEdge
        elif pos.x() >= self.width() - 1 - m:
            edges |= Qt.Edge.RightEdge
        if pos.y() <= m:
            edges |= Qt.Edge.TopEdge
        elif pos.y() >= self.height() - 1 - m:
            edges |= Qt.Edge.BottomEdge
        return edges

    @staticmethod
    def edge_cursor(edges):
        left_right = bool(edges & (Qt.Edge.LeftEdge | Qt.Edge.RightEdge))
        top_bottom = bool(edges & (Qt.Edge.TopEdge | Qt.Edge.BottomEdge))
        if left_right and top_bottom:
            falling = (edges & Qt.Edge.LeftEdge and edges & Qt.Edge.TopEdge) or (
                edges & Qt.Edge.RightEdge and edges & Qt.Edge.BottomEdge)
            return Qt.CursorShape.SizeFDiagCursor if falling else Qt.CursorShape.SizeBDiagCursor
        if left_right:
            return Qt.CursorShape.SizeHorCursor
        if top_bottom:
            return Qt.CursorShape.SizeVerCursor
        return None

    def mousePressEvent(self, event):
        if not self.frameless or event.button() != Qt.MouseButton.LeftButton:
            return
        edges = self.resize_edges(event.position().toPoint())
        handle = self.windowHandle()
        if edges and handle is not None and handle.startSystemResize(edges):
            return
        if event.position().y() < 60:
            self.begin_move(event.globalPosition().toPoint())

    def mouseMoveEvent(self, event):
        if self._drag is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag)
            return
        if self.frameless and not event.buttons():
            shape = self.edge_cursor(self.resize_edges(event.position().toPoint()))
            if shape is None:
                self.unsetCursor()
            else:
                self.setCursor(shape)

    def mouseReleaseEvent(self, event):
        self._drag = None


class PilotWindow(QWidget):
    """Remote Pilot in a larger window of its own, in the same pixel frame: a violet title band
    and the page under it. Made only when Qt WebEngine is installed (it is not in the
    PySide6-Essentials build; remote_pilot_link.WEBENGINE_NEED says what it would take)."""

    def __init__(self, url):
        from PySide6.QtWebEngineWidgets import QWebEngineView
        super().__init__()
        self.setWindowTitle('Remote Pilot')
        self.resize(1220, 900)
        self.setStyleSheet(f'background: {theme.PALETTE["window"]};')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)
        band = QFrame()
        band.setStyleSheet(f'background: {theme.PALETTE["tab_pilot"]};')
        band_layout = QHBoxLayout(band)
        band_layout.setContentsMargins(10, 4, 10, 4)
        band_layout.addWidget(PixelText('REMOTE PILOT', theme.PALETTE['window'], 2))
        band_layout.addStretch(1)
        layout.addWidget(band)
        self.view = QWebEngineView()
        self.view.setUrl(QUrl(url))
        layout.addWidget(self.view, 1)


def licence_folder(root):
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent / 'licenses'
    return Path(root) / 'scripts/chromatic_gui_licenses'


def quiet_console():
    """A windowed exe (or pythonw) has no stdout/stderr, and esptool prints progress
    ("Connecting...") to stdout: without a sink that print raises. Its chatter is dropped;
    the manager's own messages reach the window through `say`."""
    for name in ('stdout', 'stderr'):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, 'w', encoding='utf-8'))


def self_check(cm, root):
    """No device access and no writes: can this build find everything it needs?"""
    import esptool
    from serial.tools import list_ports  # noqa: F401  (imported only, no port opened)
    from PySide6.QtCore import qVersion
    pinned = []
    for folder in cm.PINNED:
        try:
            cm.Release.from_pinned(Path(folder))
            pinned.append(True)
        except Exception:
            pinned.append(False)
    return {'project_found': (Path(root) / 'scripts/chromatic_manager.py').is_file(),
            'manager_loaded_from_project': Path(cm.__file__).resolve().parent == (Path(root) / 'scripts').resolve(),
            'journal_folder_in_user_data': Path(cm.WORK).resolve().is_relative_to(data_folder(root).resolve()),
            'pinned_release_copies_valid': pinned,
            'build_folder_present': (Path(cm.RELEASE) / 'chromatic_launcher.bin').is_file(),
            'esptool': esptool.__version__, 'qt': qVersion(), 'frozen': bool(getattr(sys, 'frozen', False)),
            # The Remote Pilot tab: found (folders only looked at), its window kind, the process check works.
            'remote_pilot_found': pilot_link.locate(root) is not None,
            'remote_pilot_window': 'own window (Qt WebEngine)' if pilot_link.webengine_available() else
                                   'browser app window' if pilot_link.app_browser() else 'browser',
            'process_check': pilot_link.process_alive(os.getpid())}


def main(argv=None, root=ROOT):
    parser = argparse.ArgumentParser(description='Chromatic Manager window.')
    parser.add_argument('--auto', choices=('status',),
                        help='Start this read-only action at once and close when it ends (for checks).')
    parser.add_argument('--result-file', type=Path, help='With --auto or --self-check: write the result here.')
    parser.add_argument('--self-check', action='store_true',
                        help='Without touching the console: check this build finds the project, the manager and '
                             'the pinned launcher release, then exit.')
    parser.add_argument('--no-api', action='store_true',
                        help='Do not offer the local API (127.0.0.1) that the MCP server attaches to.')
    args = parser.parse_args(argv)
    sys.dont_write_bytecode = True
    quiet_console()
    import chromatic_manager as cm
    if args.self_check:
        found = self_check(cm, root)
        if args.result_file:
            args.result_file.write_text(json.dumps(found, indent=1), encoding='utf-8')
        ok = found['project_found'] and found['manager_loaded_from_project'] and any(found['pinned_release_copies_valid'])
        return 0 if ok else 1
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    theme.apply(app)
    from PySide6.QtGui import QIcon
    app.setWindowIcon(QIcon(theme.icon_pixmap()))
    from chromatic_lock import Busy
    from chromatic_manager_core import acquire_window_lock
    try:
        window_lock = acquire_window_lock(data_folder(root))  # held until this process ends
    except Busy:
        QMessageBox.information(None, APP_NAME, 'Chromatic Manager is already open. Use that window.')
        return 1
    window = MainWindow(cm, root=root, detect=None if args.auto else lambda: len(cm.ManagerProgrammer.ports()),
                        pilot=lambda settings: pilot_link.PilotLink.for_app(root, settings))
    api = None
    if not args.no_api:
        from chromatic_manager_api import LocalApiServer
        try:
            api = LocalApiServer(window.service).start()
            window.add_line(f'Local API for Claude/Codex (MCP) on 127.0.0.1:{api.port}. Reads are free; changes '
                            'need your OK here.')
        except OSError:
            window.add_line('The local API could not start; the window works as usual.')
    window.show()
    outcome = {}
    if args.auto:
        def finished(code):
            outcome['code'] = code
            if args.result_file:
                log = window.service.last_log_path
                args.result_file.write_text(json.dumps({'exit': code, 'messages': window.run_lines,
                                                        'next': window.next_text,
                                                        'log_file': str(log) if log else None}, indent=1),
                                            encoding='utf-8')
            QTimer.singleShot(1500, app.quit)
        window.finished.connect(finished)
        QTimer.singleShot(300, lambda: window.start(args.auto))
    app.exec()
    if api is not None:
        api.stop()  # the discovery file is removed only if it is still ours
    window.service.wait()
    window.pilot.stop()  # a Remote Pilot this window started (closeEvent normally did it already)
    window_lock.__exit__(None, None, None)
    return outcome.get('code', 0)


if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main(sys.argv[1:]))
