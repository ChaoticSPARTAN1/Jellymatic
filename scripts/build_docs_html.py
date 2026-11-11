# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Builds Open-Documentation.html: this repository's documentation as one page that opens offline.

Double-click the result: it needs no server, no internet and no add-ons. Every tracked Markdown page
(README.md, docs/*.md, CONTRIBUTING, SECURITY, THIRD-PARTY-NOTICES, ADDITIONAL-TERMS) is converted to
HTML here by the small Markdown converter below (Python standard library only) and stored inside the
page as JSON. Pictures and fonts load from the repository folder by relative path, so keep the page
next to docs/. Nothing is fetched from the internet; the page's Content-Security-Policy forbids it.
Only files tracked by Git are included (when Git is available), so nothing personal can slip in.

    py -3 scripts/build_docs_html.py            write Open-Documentation.html and docs/index.html
    py -3 scripts/build_docs_html.py --check    exit 1 if it is out of date (scripts/test_docs_html.py)

docs/index.html is the same page with paths relative to docs/, ready for GitHub Pages (branch main, folder
/docs; docs/.nojekyll makes GitHub serve the files as they are). Pictures outside docs/ are put inside it.
The PDF guide is made by build_docs_pdf.py.

Run it after editing any documentation page.
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import posixpath
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / 'Open-Documentation.html'
SCRIPT_NAME = 'scripts/build_docs_html.py'

# ---------------------------------------------------------------- this repository
PROJECT = 'Jellymatic'
THEME = 'chroma'                       # Chromatic Manager look: pixel fonts, red window, tabs
TAGLINE = 'Jellyfin on your ModRetro Chromatic'
EXTRA = ['CONTRIBUTING.md', 'SECURITY.md', 'THIRD-PARTY-NOTICES.md', 'ADDITIONAL-TERMS.md']
GROUPS = [                             # tabs; a page not named here goes under the last one
    ('Start', ['README.md', 'docs/Home.md', 'docs/What-you-need.md', 'docs/Before-you-start.md']),
    ('Guide', ['docs/Install-with-Chromatic-Manager.md', 'docs/First-run-on-the-console.md',
               'docs/Using-Jellyfin-on-the-Chromatic.md', 'docs/Hosting-Jellyfin.md', 'docs/Pocket-link.md',
               'docs/Remote-Pilot.md']),
    ('Recovery', ['docs/Recovery.md', 'docs/FAQ.md']),
    ('More', ['docs/Building-from-source.md', 'docs/AI-prompt-examples.md', 'docs/Credits-and-licences.md',
              'docs/Design-notes.md', 'CONTRIBUTING.md', 'SECURITY.md', 'THIRD-PARTY-NOTICES.md',
              'ADDITIONAL-TERMS.md']),
]
LABELS = {
    'README.md': 'Start here', 'docs/Home.md': 'Wiki home', 'docs/Using-Jellyfin-on-the-Chromatic.md': 'Using Jellyfin',
    'docs/Credits-and-licences.md': 'Credits & licences', 'docs/AI-prompt-examples.md': 'AI prompt examples',
    'docs/FAQ.md': 'FAQ', 'CONTRIBUTING.md': 'Contributing', 'SECURITY.md': 'Security',
    'THIRD-PARTY-NOTICES.md': 'Third-party notices', 'ADDITIONAL-TERMS.md': 'Additional terms',
    'docs/Before-you-start.md': 'Before you start', 'docs/Hosting-Jellyfin.md': 'Hosting Jellyfin',
    'docs/Recovery.md': 'Recovery', 'docs/Design-notes.md': 'Design notes',
}
# README-only art (GitHub strips CSS, so the README carries its theme in pictures); the page draws these itself.
SKIP_ART = re.compile(r'^docs/images/readme/(?:section-|divider)')
FONTS = {'PixelHead': 'docs/fonts/heading-PressStart2P-Regular.ttf', 'PixelBody': 'docs/fonts/body-Silkscreen-Regular.ttf'}
CSP = ("default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline' 'self'; font-src 'self'; "
       "script-src 'unsafe-inline'")


# ---------------------------------------------------------------- pages
def tracked_files():
    """Documentation files Git tracks, or None without Git (for example a ZIP download)."""
    try:
        run = subprocess.run(['git', '-C', str(ROOT), 'ls-files', '-z', '--', 'README.md', 'docs', *EXTRA],
                             capture_output=True, check=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    names = {name for name in run.stdout.decode('utf-8', 'replace').split('\0') if name}
    return names or None


def page_paths():
    found = ['README.md'] + sorted('docs/' + p.name for p in (ROOT / 'docs').glob('*.md') if not p.name.startswith('_'))
    found += [name for name in EXTRA if (ROOT / name).is_file()]
    tracked = tracked_files()
    return [p for p in found if (ROOT / p).is_file() and (tracked is None or p in tracked)]


def grouped(paths):
    left = list(paths)
    groups = []
    for name, members in GROUPS:
        items = [p for p in members if p in left]
        for p in items:
            left.remove(p)
        groups.append([name, items])
    groups[-1][1].extend(left)
    return [(name, items) for name, items in groups if items]


def label(path, text):
    if path in LABELS:
        return LABELS[path]
    m = re.search(r'^#\s+(.+?)\s*#*\s*$', text, re.M)
    if m and path != 'README.md':
        return re.sub(r'[*_`]', '', m.group(1)).strip()
    return Path(path).stem.replace('-', ' ').replace('_', ' ')


# ---------------------------------------------------------------- Markdown -> HTML
# A small converter for the Markdown GitHub renders in this repository: headings (with GitHub's
# anchor names), paragraphs, ordered and bullet lists (nested, tight and loose), fenced code,
# tables with alignment, block quotes, rules, inline code, emphasis, links, images, bare URLs and
# raw HTML (including <picture>/<source srcset>). Unknown <words> are shown as text.
PUNCT = set('!"#$%&\'()*+,-./:;<=>?@[\\]^_`{|}~')
BLOCK_HTML = {'address', 'article', 'aside', 'blockquote', 'center', 'details', 'div', 'dl', 'fieldset', 'figcaption',
              'figure', 'footer', 'form', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'header', 'hr', 'li', 'main', 'nav',
              'ol', 'p', 'picture', 'pre', 'section', 'summary', 'table', 'tbody', 'td', 'tfoot', 'th', 'thead',
              'tr', 'ul'}
INLINE_HTML = BLOCK_HTML | {'a', 'abbr', 'b', 'br', 'code', 'del', 'em', 'i', 'img', 'ins', 'kbd', 'mark', 's',
                            'small', 'source', 'span', 'strong', 'sub', 'sup', 'u'}
FENCE = re.compile(r'^( {0,3})(`{3,}|~{3,})\s*([^`\s]*)[^`]*$')
HEADING = re.compile(r'^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$')
RULE = re.compile(r'^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$')
ITEM = re.compile(r'^( {0,3})([-*+]|\d{1,9}[.)])([ \t]+|$)')
DELIM = re.compile(r'^ {0,3}\|?[ \t]*:?-+:?[ \t]*(?:\|[ \t]*:?-+:?[ \t]*)*\|?[ \t]*$')
HTML_START = re.compile(r'^ {0,3}</?([A-Za-z][A-Za-z0-9-]*)(?=[\s/>]|$)')
TAG = re.compile(r'<(/?)([A-Za-z][A-Za-z0-9-]*)((?:\s+[A-Za-z_:][\w.:-]*(?:\s*=\s*(?:"[^"]*"|\'[^\']*\'|[^\s"\'=<>`]+))?)*)\s*(/?)>')
ATTR = re.compile(r'([A-Za-z_:][\w.:-]*)(?:\s*=\s*("[^"]*"|\'[^\']*\'|[^\s"\'=<>`]+))?')
SCHEME = re.compile(r'^[A-Za-z][A-Za-z0-9+.-]*:')
BARE_URL = re.compile(r'https?://[^\s<>"]+')


MIME = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.gif': 'image/gif', '.svg': 'image/svg+xml',
        '.webp': 'image/webp'}


def github_url(path):
    """A repository file or folder on GitHub (for copies of the docs that are not next to the files)."""
    kind = 'tree' if (ROOT / path).is_dir() else 'blob'
    return f'https://github.com/ChaoticSPARTAN1/{PROJECT}/{kind}/main/{path.rstrip("/")}'


class Page:
    """One Markdown page. variant: 'root' (Open-Documentation.html in the main folder), 'docs'
    (docs/index.html for GitHub Pages, which serves only docs/) or 'print' (the PDF guide)."""

    def __init__(self, path, pages, variant='root'):
        self.path, self.pages, self.slugs, self.variant = path, pages, {}, variant

    def src(self, url):
        """A picture path, relative to where the built page lives."""
        if not url or SCHEME.match(url) or url.startswith(('/', '#')):
            return url
        path = posixpath.normpath(posixpath.join(posixpath.dirname(self.path), url))
        if self.variant != 'docs':
            return path
        if path.startswith('docs/'):
            return path[5:]
        file = ROOT / path                     # outside docs/: GitHub Pages cannot serve it, so it goes inline
        if file.is_file() and file.suffix.lower() in MIME:
            return f'data:{MIME[file.suffix.lower()]};base64,' + base64.b64encode(file.read_bytes()).decode('ascii')
        return path

    def href(self, url):
        """(href, opens in a new tab). Pages switch inside the page; other files open in a new tab."""
        if not url:
            return url, False
        if url.startswith('#'):
            return f'#{self.path}:{url[1:]}', False
        if SCHEME.match(url):
            return url, url.lower().startswith(('http:', 'https:'))
        path, _, frag = url.partition('#')
        target = posixpath.normpath(posixpath.join(posixpath.dirname(self.path), path)) if path else self.path
        if target in self.pages:
            return f'#{target}' + (f':{frag}' if frag else ''), False
        if self.variant != 'root':
            return github_url(target), True
        return target + ('/' if path.endswith('/') else ''), True

    def slug(self, text):
        base = re.sub(r'[^\w\- ]', '', text.strip().lower()).replace(' ', '-')
        n = self.slugs.get(base, 0)
        self.slugs[base] = n + 1
        return base if n == 0 else f'{base}-{n}'


def esc(text):
    return html.escape(text, quote=False)


def esc_text(text):
    """Escape <, > and & but keep entity references such as &copy; as they are."""
    text = re.sub(r'&(?!#\d+;|#[xX][0-9a-fA-F]+;|[A-Za-z][A-Za-z0-9]*;)', '&amp;', text)
    return text.replace('<', '&lt;').replace('>', '&gt;')


def plain(fragment):
    return html.unescape(re.sub(r'<[^>]*>', '', fragment))


def fix_tag(match, page):
    """Rewrites one HTML tag: picture paths, page links, new-tab links; drops light-theme <source>."""
    closing, name, attrs, selfclose = match.group(1), match.group(2).lower(), match.group(3) or '', match.group(4)
    if closing:
        return f'</{name}>'
    pairs = []
    for key, raw in ATTR.findall(attrs):
        key = key.lower()
        value = None if raw == '' else html.unescape(raw[1:-1] if raw[:1] in '"\'' else raw)
        pairs.append([key, value])
    values = {k: v for k, v in pairs}
    if name == 'source' and 'prefers-color-scheme: light' in (values.get('media') or '').replace('  ', ' '):
        return ''                      # the page is always dark: use the dark picture
    external = False
    for pair in pairs:
        key, value = pair
        if value is None:
            continue
        if key == 'src':
            pair[1] = page.src(value)
        elif key == 'srcset':
            parts = []
            for part in value.split(','):
                bits = part.strip().split(None, 1)
                if bits:
                    parts.append(' '.join([page.src(bits[0])] + bits[1:]))
            pair[1] = ', '.join(parts)
        elif key == 'href' and name == 'a':
            pair[1], external = page.href(value)
    if name == 'a' and external:
        pairs = [p for p in pairs if p[0] not in ('target', 'rel')] + [['target', '_blank'], ['rel', 'noopener noreferrer']]
    out = '<' + name
    for key, value in pairs:
        out += f' {key}' if value is None else f' {key}="{html.escape(value, quote=True)}"'
    return out + (' /' if selfclose else '') + '>'


def raw_html(text, page):
    """A raw HTML block, or None when it holds only README-only art."""
    images = re.findall(r'<(?:img|source)\b[^>]*?\s(?:src|srcset)\s*=\s*"([^"]*)"', text, re.I)
    if images and all(SKIP_ART.match(page.src(src)) for src in images):
        return None
    text = re.sub(r'<!--.*?-->', '', text, flags=re.S)
    return TAG.sub(lambda m: fix_tag(m, page), text).strip()


def link_end(text, start):
    """Index after the ']' that closes the '[' at start, or -1."""
    depth, i = 0, start
    while i < len(text):
        c = text[i]
        if c == '\\':
            i += 2
            continue
        if c == '`':
            run = len(text[i:]) - len(text[i:].lstrip('`'))
            close = text.find('`' * run, i + run)
            if close >= 0:
                i = close + run
                continue
        if c == '[':
            depth += 1
        elif c == ']':
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return -1


def link_target(text, start):
    """Parses '(url "title")' at start; returns (url, title, index after) or None."""
    if start >= len(text) or text[start] != '(':
        return None
    i = start + 1
    while i < len(text) and text[i] in ' \t\n':
        i += 1
    if i < len(text) and text[i] == '<':
        close = text.find('>', i)
        if close < 0:
            return None
        url, i = text[i + 1:close], close + 1
    else:
        depth, j = 0, i
        while j < len(text) and not text[j].isspace():
            if text[j] == '\\':
                j += 2
                continue
            if text[j] == '(':
                depth += 1
            elif text[j] == ')':
                if depth == 0:
                    break
                depth -= 1
            j += 1
        url, i = text[i:j], j
    while i < len(text) and text[i] in ' \t\n':
        i += 1
    title = None
    if i < len(text) and text[i] in '"\'':
        close = text.find(text[i], i + 1)
        if close < 0:
            return None
        title, i = text[i + 1:close], close + 1
        while i < len(text) and text[i] in ' \t\n':
            i += 1
    if i >= len(text) or text[i] != ')':
        return None
    return re.sub(r'\\(.)', r'\1', url), title, i + 1


EMPHASIS = [
    (re.compile(r'\*\*\*(?=\S)(.+?)(?<=\S)\*\*\*', re.S), r'<em><strong>\1</strong></em>'),
    (re.compile(r'\*\*(?=\S)(.+?)(?<=\S)\*\*', re.S), r'<strong>\1</strong>'),
    (re.compile(r'(?<![\w])__(?=\S)(.+?)(?<=\S)__(?![\w])', re.S), r'<strong>\1</strong>'),
    (re.compile(r'(?<!\*)\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?!\*)', re.S), r'<em>\1</em>'),
    (re.compile(r'(?<![\w])_(?=[^\s_])(.+?)(?<=[^\s_])_(?![\w])', re.S), r'<em>\1</em>'),
    (re.compile(r'~~(?=\S)(.+?)(?<=\S)~~', re.S), r'<del>\1</del>'),
]


def inline(text, page, in_link=False):
    kept = []

    def keep(fragment):
        kept.append(fragment)
        return f'\x00{len(kept) - 1}\x00'

    text = re.sub(r'(?: {2,}|\\)\n', '\x02', text.strip())
    buf, i = [], 0
    while i < len(text):
        c = text[i]
        if c == '\x02':
            buf.append(keep('<br>\n'))
            i += 1
        elif c == '\\' and i + 1 < len(text) and text[i + 1] in PUNCT:
            buf.append(keep(esc(text[i + 1])))
            i += 2
        elif c == '`':
            run = len(text[i:]) - len(text[i:].lstrip('`'))
            close = i + run
            while True:
                close = text.find('`' * run, close)
                if close < 0 or not text.startswith('`', close + run):
                    break
                close += len(text[close:]) - len(text[close:].lstrip('`'))
            if close < 0:
                buf.append(text[i:i + run])
                i += run
                continue
            code = text[i + run:close].replace('\n', ' ')
            if len(code) > 2 and code[0] == ' ' and code[-1] == ' ' and code.strip():
                code = code[1:-1]
            buf.append(keep(f'<code>{esc(code)}</code>'))
            i = close + run
        elif c == '[' or (c == '!' and text.startswith('[', i + 1)):
            image = c == '!'
            start = i + 1 if image else i
            end = link_end(text, start)
            target = link_target(text, end) if end > 0 else None
            if target is None:
                buf.append(c)
                i += 1
                continue
            url, title, after = target
            inner = text[start + 1:end - 1]
            title_attr = f' title="{html.escape(title, quote=True)}"' if title else ''
            if image:
                alt = html.escape(plain(inline(inner, page)), quote=True)
                buf.append(keep(f'<img src="{html.escape(page.src(url), quote=True)}" alt="{alt}"{title_attr}>'))
            else:
                href, external = page.href(url)
                extra = ' target="_blank" rel="noopener noreferrer"' if external else ''
                buf.append(keep(f'<a href="{html.escape(href, quote=True)}"{title_attr}{extra}>{inline(inner, page, True)}</a>'))
            i = after
        elif c == '<':
            auto = re.match(r'<((?:https?|mailto):[^\s<>]+)>', text[i:])
            tag = TAG.match(text, i)
            if auto:
                href, external = page.href(auto.group(1))
                extra = ' target="_blank" rel="noopener noreferrer"' if external else ''
                buf.append(keep(f'<a href="{html.escape(href, quote=True)}"{extra}>{esc(auto.group(1))}</a>'))
                i += auto.end()
            elif text.startswith('<!--', i) and '-->' in text[i:]:
                i = text.index('-->', i) + 3
            elif tag and tag.group(2).lower() in INLINE_HTML:
                buf.append(keep(fix_tag(tag, page)))
                i = tag.end()
            else:
                buf.append(c)
                i += 1
        elif c == 'h' and not in_link and (i == 0 or not text[i - 1].isalnum()) and BARE_URL.match(text, i):
            url = BARE_URL.match(text, i).group(0)
            while url and url[-1] in '.,:;!?*_\'"':
                url = url[:-1]
            if url.endswith(')') and url.count('(') < url.count(')'):
                url = url[:-1]
            href, external = page.href(url)
            buf.append(keep(f'<a href="{html.escape(href, quote=True)}" target="_blank" rel="noopener noreferrer">{esc(url)}</a>'))
            i += len(url)
        else:
            buf.append(c)
            i += 1
    out = esc_text(''.join(buf))
    for pattern, repl in EMPHASIS:
        out = pattern.sub(repl, out)
    while '\x00' in out:
        out = re.sub(r'\x00(\d+)\x00', lambda m: kept[int(m.group(1))], out)
    return out


def indent_of(line):
    return len(line) - len(line.lstrip(' '))


def starts_block(line, in_paragraph=True):
    """Does this line start a new block (and so end a paragraph)?"""
    if FENCE.match(line) or HEADING.match(line) or RULE.match(line) or line.lstrip().startswith('>'):
        return True
    m = HTML_START.match(line)
    if m and m.group(1).lower() in BLOCK_HTML:
        return True
    item = ITEM.match(line)
    if item and line[item.end():].strip():
        marker = item.group(2)
        return not in_paragraph or not marker[0].isdigit() or marker[:-1] == '1'
    return False


def split_row(line):
    s = line.strip()
    if s.startswith('|'):
        s = s[1:]
    if s.endswith('|') and not s.endswith('\\|'):
        s = s[:-1]
    cells, cur, i, code = [], '', 0, 0
    while i < len(s):
        c = s[i]
        if c == '\\' and i + 1 < len(s) and s[i + 1] == '|':
            cur += '|'
            i += 2
            continue
        if c == '`':
            code ^= 1
        if c == '|' and not code:
            cells.append(cur.strip())
            cur = ''
        else:
            cur += c
        i += 1
    cells.append(cur.strip())
    return cells


def table(lines, page):
    head = split_row(lines[0])
    aligns = []
    for cell in split_row(lines[1]):
        left, right = cell.startswith(':'), cell.endswith(':')
        aligns.append('center' if left and right else 'right' if right else 'left' if left else None)

    def row(cells, tag):
        cells = (cells + [''] * len(head))[:len(head)]
        out = []
        for n, cell in enumerate(cells):
            align = f' align="{aligns[n]}"' if n < len(aligns) and aligns[n] else ''
            out.append(f'<{tag}{align}>{inline(cell, page)}</{tag}>')
        return '<tr>\n' + '\n'.join(out) + '\n</tr>'

    body = [row(split_row(line), 'td') for line in lines[2:]]
    out = '<div class="table"><table>\n<thead>\n' + row(head, 'th') + '\n</thead>\n'
    if body:
        out += '<tbody>\n' + '\n'.join(body) + '\n</tbody>\n'
    return out + '</table></div>'


def list_block(lines, i, page):
    first = ITEM.match(lines[i])
    ordered = first.group(2)[0].isdigit()
    kind = first.group(2)[-1]
    items, loose = [], False
    while i < len(lines):
        m = ITEM.match(lines[i])
        if not m or m.group(2)[0].isdigit() != ordered or m.group(2)[-1] != kind or RULE.match(lines[i]):
            break
        spaces = len(m.group(3).expandtabs(4))
        width = len(m.group(1)) + len(m.group(2)) + (spaces if 1 <= spaces <= 4 else 1)
        body = [lines[i][m.end():] if 1 <= spaces <= 4 else lines[i][len(m.group(1)) + len(m.group(2)) + 1:]]
        j = i + 1
        while j < len(lines):
            line = lines[j]
            if not line.strip():
                body.append('')
            elif indent_of(line) >= width:
                body.append(line[width:])
            elif body[-1].strip() and not starts_block(line, in_paragraph=False) and not FENCE.match(body[-1]):
                body.append(line.strip())          # lazy continuation of the item's paragraph
            else:
                break
            j += 1
        while body and not body[-1].strip():
            body.pop()
            if j < len(lines) and ITEM.match(lines[j]):
                loose = True                       # a blank line between two items
        fenced = False
        for line in body:
            if FENCE.match(line):
                fenced = not fenced
            elif not line.strip() and not fenced:
                loose = True                       # a blank line between two blocks of one item
        items.append(body)
        i = j
        while i < len(lines) and not lines[i].strip():
            i += 1
        if i < len(lines) and not ITEM.match(lines[i]):
            break
        if i < len(lines):
            m2 = ITEM.match(lines[i])
            if m2.group(2)[0].isdigit() != ordered or m2.group(2)[-1] != kind:
                break
    tag = 'ol' if ordered else 'ul'
    start = int(first.group(2)[:-1]) if ordered else 1
    attrs = f' start="{start}"' if ordered and start != 1 else ''
    rendered = [f'<li>{blocks(body, page, tight=not loose)}</li>' for body in items]
    return f'<{tag}{attrs}>\n' + '\n'.join(rendered) + f'\n</{tag}>', i


def blocks(lines, page, tight=False):
    out, i = [], 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        fence = FENCE.match(line)
        if fence:
            pad, mark, lang = len(fence.group(1)), fence.group(2), fence.group(3)
            closing = re.compile(r'^ {0,3}' + re.escape(mark[0]) + '{' + str(len(mark)) + r',}[ \t]*$')
            code, i = [], i + 1
            while i < len(lines) and not closing.match(lines[i]):
                code.append(lines[i][min(pad, indent_of(lines[i])):])
                i += 1
            i += 1
            cls = f' class="language-{html.escape(lang, quote=True)}"' if lang else ''
            out.append(f'<pre><code{cls}>' + esc('\n'.join(code) + '\n' if code else '') + '</code></pre>')
            continue
        if indent_of(line) >= 4:
            code = []
            while i < len(lines) and (indent_of(lines[i]) >= 4 or not lines[i].strip()):
                code.append(lines[i][4:])
                i += 1
            while code and not code[-1].strip():
                code.pop()
            out.append('<pre><code>' + esc('\n'.join(code) + '\n') + '</code></pre>')
            continue
        if line.lstrip().startswith('<!--'):
            while i < len(lines) and '-->' not in lines[i]:
                i += 1
            i += 1
            continue
        start = HTML_START.match(line)
        if start and start.group(1).lower() in BLOCK_HTML:
            chunk = []
            while i < len(lines) and lines[i].strip():
                chunk.append(lines[i])
                i += 1
            fragment = raw_html('\n'.join(chunk), page)
            if fragment:
                out.append(fragment)
            continue
        heading = HEADING.match(line)
        if heading:
            level, content = len(heading.group(1)), inline(heading.group(2) or '', page)
            out.append(f'<h{level} id="h-{html.escape(page.slug(plain(content)), quote=True)}">{content}</h{level}>')
            i += 1
            continue
        if RULE.match(line):
            out.append('<hr>')
            i += 1
            continue
        if line.lstrip().startswith('>'):
            quoted = []
            while i < len(lines) and lines[i].strip():
                current = lines[i].lstrip()
                if current.startswith('>'):
                    quoted.append(current[2:] if current.startswith('> ') else current[1:])
                elif quoted and quoted[-1].strip() and not starts_block(lines[i]):
                    quoted.append(current)
                else:
                    break
                i += 1
            out.append('<blockquote>\n' + blocks(quoted, page) + '\n</blockquote>')
            continue
        if '|' in line and i + 1 < len(lines) and DELIM.match(lines[i + 1]) and '-' in lines[i + 1]:
            rows = [line, lines[i + 1]]
            i += 2
            while i < len(lines) and lines[i].strip() and not starts_block(lines[i]):
                rows.append(lines[i])
                i += 1
            out.append(table(rows, page))
            continue
        if ITEM.match(line) and line[ITEM.match(line).end():].strip():
            fragment, i = list_block(lines, i, page)
            out.append(fragment)
            continue
        para = [line.strip() if not line.endswith('  ') else line.lstrip()]
        i += 1
        while i < len(lines) and lines[i].strip() and not starts_block(lines[i]):
            if '|' in lines[i] and i + 1 < len(lines) and DELIM.match(lines[i + 1]) and '-' in lines[i + 1]:
                break
            para.append(lines[i].lstrip())
            i += 1
        text = inline('\n'.join(para), page)
        out.append(text if tight else f'<p>{text}</p>')
    return '\n'.join(out)


def markdown(text, path, pages, variant='root'):
    lines = text.replace('\r\n', '\n').replace('\r', '\n').expandtabs(4).split('\n')
    return blocks(lines, Page(path, set(pages), variant))


# ---------------------------------------------------------------- page template
STYLE_BASE = """
* { box-sizing:border-box; }
html, body { margin:0; }
a { cursor:pointer; }
.md { overflow-wrap:break-word; word-wrap:break-word; }
.md > :first-child { margin-top:0 !important; }
.md p, .md ul, .md ol, .md pre, .md blockquote { margin:0 0 16px; }
.md ul, .md ol { padding-left:2em; }
.md li + li { margin-top:.3em; }
.md li > p { margin:.6em 0; }
.md li > ul, .md li > ol { margin:.3em 0 0; }
.md h1, .md h2, .md h3, .md h4, .md h5, .md h6 { margin:28px 0 16px; line-height:1.3; }
.md img { max-width:100%; height:auto; vertical-align:middle; }
.md .table { overflow-x:auto; margin:0 0 16px; }
.md table { width:100%; border-collapse:collapse; margin:0; }
.md td code { word-break:break-word; }
.md th, .md td { padding:6px 13px; vertical-align:top; }
.md pre { padding:16px; overflow:auto; line-height:1.45; }
.md code { padding:.15em .4em; }
.md pre code { padding:0; white-space:pre; display:block; }
.md hr { margin:24px 0; }
.md blockquote { margin-left:0; margin-right:0; }
.md blockquote > :last-child { margin-bottom:0; }
.md picture { display:inline-block; max-width:100%; }
noscript p { padding:24px; font-family:sans-serif; }
"""

STYLE_CHROMA = """
body.chroma {
  --accent:#ff1a1a; --green:#3cff3c; --yellow:#ffd93c; --grey:#c8c8c8; --grey-hi:#f0f0f0; --grey-lo:#5c5c5c;
  --frame-in:#6e6e6e; --text:#f4f4f4; --muted:#a8a8a8;
  min-height:100vh; padding:18px 18px 28px; background-color:#1a0820;
  background-image:repeating-linear-gradient(135deg, #3a0a4f 0px, #5b1470 120px, #8a1838 240px, #c0263a 300px, #8a1838 360px, #5b1470 480px, #3a0a4f 600px);
  background-attachment:fixed; color:var(--text); font-family:"PixelBody", monospace;
}
.shell { max-width:1240px; margin:0 auto; }
#tabs { display:flex; flex-wrap:wrap; gap:6px; align-items:flex-end; padding-left:22px; }
#tabs a { font-family:"PixelHead", monospace; font-size:10px; color:#fff; background:#3a3a3a; padding:10px 14px 8px; text-decoration:none;
  border:3px solid #111; border-bottom:0; box-shadow:inset 3px 3px 0 #5a5a5a, inset -3px 0 0 #222; text-transform:uppercase; }
#tabs a:hover { background:#4a4a4a; }
#tabs a.on { background:#000; border-color:var(--accent); padding-top:13px; box-shadow:inset 0 3px 0 var(--accent); }
.window { border:6px solid var(--accent); background:#000; box-shadow:6px 6px 0 rgba(0,0,0,.45); }
.titlebar { margin:8px 8px 0; background:var(--accent); border:3px solid #000; outline:3px solid var(--accent);
  display:flex; align-items:center; justify-content:space-between; gap:12px; padding:10px 14px; }
.titlebar h1 { margin:0; font-family:"PixelHead", monospace; font-size:22px; font-weight:normal; color:#000; letter-spacing:2px;
  text-transform:uppercase; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; min-width:0; }
.dither { width:48px; height:32px; flex:none; background:repeating-conic-gradient(#000 0 25%, transparent 0 50%) 0 0/8px 8px; }
.body { display:flex; gap:14px; padding:14px; }
#pages { width:260px; flex:none; display:flex; flex-direction:column; gap:6px; align-self:flex-start; position:sticky; top:8px;
  max-height:calc(100vh - 16px); overflow:auto; padding-right:4px; }
#pages .group { font-family:"PixelHead", monospace; font-size:9px; color:var(--yellow); margin:10px 0 2px; letter-spacing:1px; text-transform:uppercase; }
#pages a { display:block; font-family:"PixelHead", monospace; font-size:9px; line-height:1.4; color:#111; background:var(--grey); text-transform:uppercase;
  text-decoration:none; padding:8px 10px; border:2px solid #000; box-shadow:inset 3px 3px 0 var(--grey-hi), inset -3px -3px 0 var(--grey-lo); word-break:break-word; }
#pages a:hover { background:#dcdcdc; }
#pages a.on { background:#7ee05a; box-shadow:inset 3px 3px 0 #c8ffb0, inset -3px -3px 0 #2e7a1c; }
.panel { flex:1; min-width:0; border:4px solid var(--frame-in); box-shadow:inset 3px 3px 0 #2a2a2a; background:#000; padding:24px 30px; }
.crumb { font-family:"PixelHead", monospace; font-size:9px; color:var(--muted); margin-bottom:14px; text-transform:uppercase; word-break:break-all; }
.footer { display:flex; flex-wrap:wrap; gap:8px; justify-content:space-between; align-items:center; padding:0 18px 12px; }
.footer .credit { font-family:"PixelHead", monospace; font-size:9px; color:#cfcfcf; }
.footer .mark { font-family:"PixelHead", monospace; font-size:20px; }
.rainbow span:nth-child(7n+1){color:#ff4d4d} .rainbow span:nth-child(7n+2){color:#ff9a3c} .rainbow span:nth-child(7n+3){color:#ffd93c}
.rainbow span:nth-child(7n+4){color:#7ee05a} .rainbow span:nth-child(7n+5){color:#3cc8ff} .rainbow span:nth-child(7n+6){color:#8f7bff}
.rainbow span:nth-child(7n){color:#ff6ad5}
.md { color:var(--text); font-size:16px; line-height:1.75; }
.md h1, .md h2, .md h3, .md h4, .md h5, .md h6 { font-family:"PixelHead", monospace; font-weight:normal; line-height:1.6; }
.md h1 { font-size:20px; color:var(--green); text-align:center; }
.md h2 { font-size:14px; color:#000; background:var(--accent); padding:8px 12px; border:3px solid #000; outline:2px solid var(--accent); text-transform:uppercase; margin-left:2px; margin-right:2px; }
.md h3 { font-size:12px; color:var(--yellow); }
.md h4, .md h5, .md h6 { font-size:11px; color:var(--green); }
.md a { color:var(--green); text-decoration:underline; text-underline-offset:3px; }
.md strong { color:var(--yellow); }
.md code { background:#1b1b1b; color:var(--green); font-family:Consolas, "Courier New", monospace; font-size:85%; }
.md pre { background:#0c0c0c; border:3px solid var(--frame-in); }
.md pre code { color:var(--green); background:transparent; font-size:14px; }
.md table { border-collapse:separate; border-spacing:0; }
.md tr { background:#000; } .md tr:nth-child(2n) { background:#111; }
.md th, .md td { border:2px solid #444; }
.md th { background:var(--grey); color:#111; font-family:"PixelHead", monospace; font-weight:normal; font-size:9px; text-transform:uppercase;
  box-shadow:inset 2px 2px 0 var(--grey-hi), inset -2px -2px 0 var(--grey-lo); }
.md blockquote { color:var(--yellow); border-left:6px solid var(--yellow); background:#141100; padding:8px 14px; }
.md blockquote strong { color:#fff; }
.md hr { height:4px; border:0; background:repeating-linear-gradient(90deg,#ff4d4d 0 8px,#ff9a3c 8px 16px,#ffd93c 16px 24px,#7ee05a 24px 32px,#3cc8ff 32px 40px,#8f7bff 40px 48px,#ff6ad5 48px 56px); }
.md img { image-rendering:pixelated; }
.md kbd { background:var(--grey); color:#111; border:2px solid #000; box-shadow:inset -2px -2px 0 var(--grey-lo); padding:0 4px; }
@media (max-width:800px) {
  body.chroma { padding:8px; }
  .body { flex-direction:column; }
  #tabs { padding-left:6px; }
  #pages { width:auto; position:static; max-height:none; flex-direction:row; flex-wrap:wrap; }
  #pages .group { width:100%; }
  .titlebar h1 { font-size:14px; }
  .panel { padding:14px; }
  .md h1 { font-size:14px; } .md h2 { font-size:12px; }
  .md h1, .md h2, .md h3 { overflow-wrap:normal; word-break:normal; hyphens:none; }
}
"""

STYLE_MINIMAL = """
body.minimal { background:#000; color:#f2f2f2; font-family:"Helvetica Neue", Helvetica, Arial, sans-serif; min-height:100vh; padding:24px; }
.shell { max-width:1200px; margin:0 auto; }
#tabs { display:none; }
.titlebar { display:flex; align-items:center; justify-content:space-between; gap:16px; border-bottom:1px solid #262626; padding-bottom:14px; margin-bottom:22px; }
.titlebar h1 { margin:0; font-size:26px; font-weight:600; letter-spacing:-.01em; }
.titlebar .brand { color:#777; font-size:13px; letter-spacing:.12em; text-transform:uppercase; white-space:nowrap; }
.dither { display:none; }
.body { display:flex; gap:32px; }
#pages { width:240px; flex:none; display:flex; flex-direction:column; gap:2px; align-self:flex-start; position:sticky; top:16px;
  max-height:calc(100vh - 32px); overflow:auto; }
#pages .group { color:#777; font-size:11px; letter-spacing:.12em; text-transform:uppercase; margin:16px 0 6px 12px; }
#pages .group:first-child { margin-top:0; }
#pages a { color:#cfcfcf; padding:6px 12px; border-radius:999px; font-size:14px; text-decoration:none; word-break:break-word; }
#pages a:hover { background:#141414; }
#pages a.on { background:#f2f2f2; color:#000; font-weight:600; }
.panel { flex:1; min-width:0; }
.crumb { color:#777; font-size:12px; margin-bottom:14px; word-break:break-all; }
.footer { display:flex; flex-wrap:wrap; gap:8px; justify-content:space-between; color:#777; font-size:12px; border-top:1px solid #262626; margin-top:32px; padding-top:14px; letter-spacing:.06em; }
.md { color:#e8e8e8; font-size:16px; line-height:1.7; }
.md h1, .md h2 { color:#fff; border-bottom:1px solid #262626; font-weight:600; padding-bottom:.3em; }
.md h1 { font-size:2em; } .md h2 { font-size:1.5em; } .md h3 { font-size:1.25em; color:#fff; font-weight:600; }
.md h4, .md h5, .md h6 { color:#fff; font-weight:600; }
.md a { color:#fff; text-decoration:underline; text-underline-offset:3px; text-decoration-color:#777; }
.md a:hover { text-decoration-color:#fff; }
.md strong { color:#fff; }
.md code { background:#161616; border-radius:6px; font-family:Consolas, "SFMono-Regular", Menlo, monospace; font-size:85%; }
.md pre { background:#0d0d0d; border:1px solid #262626; border-radius:8px; }
.md pre code { background:transparent; font-size:13.5px; }
.md th, .md td { border:1px solid #262626; }
.md th { background:#0b0b0b; font-weight:600; }
.md tr { background:#000; } .md tr:nth-child(2n) { background:#0b0b0b; }
.md blockquote { color:#bdbdbd; border-left:3px solid #f2f2f2; padding:2px 16px; }
.md hr { height:1px; border:0; background:#262626; }
.md kbd { border:1px solid #444; border-radius:6px; padding:0 5px; font-size:85%; }
@media (max-width:800px) {
  body.minimal { padding:14px 16px; }
  .body { flex-direction:column; gap:12px; }
  #pages { width:auto; position:static; max-height:none; flex-direction:row; flex-wrap:wrap; }
  #pages .group { width:100%; margin-left:0; }
  .titlebar h1 { font-size:20px; }
}
"""

SCRIPT = """
(function () {
  var data = JSON.parse(document.getElementById('docs-data').textContent);
  var pages = {}, order = [];
  data.groups.forEach(function (g) { g.pages.forEach(function (p) { p.group = g.name; pages[p.path] = p; order.push(p.path); }); });
  var current = null;
  function el(id) { return document.getElementById(id); }
  function link(text, hash, cls) {
    var a = document.createElement('a'); a.textContent = text; a.href = '#' + hash; if (cls) a.className = cls; return a;
  }
  function parse() {
    var h = location.hash.slice(1);
    try { h = decodeURIComponent(h); } catch (e) {}
    var cut = h.indexOf(':'), path = cut < 0 ? h : h.slice(0, cut), anchor = cut < 0 ? '' : h.slice(cut + 1);
    if (!pages[path]) { path = order[0]; anchor = ''; }
    return { path: path, anchor: anchor };
  }
  function nav(page) {
    var tabs = el('tabs'), list = el('pages');
    tabs.textContent = ''; list.textContent = '';
    data.groups.forEach(function (g) {
      tabs.appendChild(link(g.name, g.pages[0].path, g.name === page.group ? 'on' : ''));
      if (data.theme === 'chroma' && g.name !== page.group) return;
      var head = document.createElement('div'); head.className = 'group'; head.textContent = g.name; list.appendChild(head);
      g.pages.forEach(function (p) { list.appendChild(link(p.title, p.path, p.path === page.path ? 'on' : '')); });
    });
  }
  function show() {
    var where = parse(), page = pages[where.path];
    if (current !== page) {
      el('page').innerHTML = page.html;
      el('title').textContent = page.title;
      el('crumb').textContent = data.project + ' / ' + page.path;
      document.title = page.title + ' \\u00b7 ' + data.project + ' documentation';
      nav(page);
      current = page;
    }
    var target = where.anchor && document.getElementById('h-' + where.anchor.toLowerCase());
    if (target) target.scrollIntoView(); else window.scrollTo(0, 0);
  }
  window.addEventListener('hashchange', show);
  show();
})();
"""


OUTPUTS = {'root': OUT, 'docs': ROOT / 'docs/index.html'}   # docs/index.html: ready for GitHub Pages (main, /docs)


def place(path, variant):
    """A repository-root path as the built page in that variant refers to it."""
    return path[5:] if variant == 'docs' and path.startswith('docs/') else path


def font_faces(variant='root'):
    return ''.join(f'@font-face {{ font-family:"{name}"; src:url("{place(path, variant)}") format("truetype"); '
                   f'font-display:swap; }}\n' for name, path in FONTS.items()) if THEME == 'chroma' else ''


def page_texts():
    """[(path, Markdown text)] of every embedded page, in reading order."""
    return [(path, (ROOT / path).read_text(encoding='utf-8').replace('\r\n', '\n'))
            for _, members in grouped(page_paths()) for path in members]


def source_hash():
    """SHA-256 of what the PDF guide is made from: the pages and the two build scripts."""
    digest = hashlib.sha256()
    for script in ('build_docs_html.py', 'build_docs_pdf.py'):
        file = Path(__file__).with_name(script)
        if file.is_file():
            digest.update(script.encode() + b'\0' + file.read_bytes().replace(b'\r\n', b'\n') + b'\0')
    for path, text in page_texts():
        digest.update(path.encode() + b'\0' + text.encode('utf-8') + b'\0')
    return digest.hexdigest()


def build(variant='root'):
    paths = page_paths()
    groups = []
    for name, members in grouped(paths):
        items = []
        for path in members:
            text = (ROOT / path).read_text(encoding='utf-8')
            items.append({'path': path, 'title': label(path, text), 'html': markdown(text, path, paths, variant)})
        groups.append({'name': name, 'pages': items})
    data = {'project': PROJECT, 'theme': THEME, 'groups': groups}
    blob = json.dumps(data, ensure_ascii=False, indent=0, separators=(',', ':'))
    blob = blob.replace('&', '\\u0026').replace('<', '\\u003c').replace('>', '\\u003e')
    wordmark = ''.join(f'<span>{esc(ch)}</span>' for ch in PROJECT)
    if THEME == 'chroma':
        footer = (f'<div class="footer"><span class="credit">CREATED BY CHAOTICSPARTAN1</span>'
                  f'<span class="mark rainbow" aria-label="{PROJECT}">{wordmark}</span></div>')
        brand = ''
    else:
        footer = f'<div class="footer"><span>Created by ChaoticSPARTAN1</span><span>{esc(PROJECT)} · {esc(TAGLINE)}</span></div>'
        brand = f'<span class="brand">{esc(PROJECT)}</span>'
    style = STYLE_BASE + (STYLE_CHROMA if THEME == 'chroma' else STYLE_MINIMAL)
    where = 'docs/index.html (for GitHub Pages: branch main, folder /docs)' if variant == 'docs' else 'Open-Documentation.html'
    return f"""<!doctype html>
<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- {where}. Built by {SCRIPT_NAME} from the repository's Markdown. Do not edit by hand: edit the .md files and run it again. -->
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="{CSP}">
<meta name="referrer" content="no-referrer">
<title>{esc(PROJECT)} documentation</title>
<style>
{font_faces(variant)}{style.strip()}
</style>
</head>
<body class="{THEME}">
<noscript><p>This page needs JavaScript to switch pages. You can also read the .md files in the docs folder.</p></noscript>
<div class="shell">
<nav id="tabs" aria-label="Sections"></nav>
<div class="window">
<header class="titlebar"><h1 id="title">{esc(PROJECT)}</h1>{brand}<div class="dither" aria-hidden="true"></div></header>
<div class="body">
<nav id="pages" aria-label="Pages"></nav>
<main class="panel"><div class="crumb" id="crumb"></div><article class="md" id="page"></article></main>
</div>
{footer}
</div>
</div>
<script type="application/json" id="docs-data">
{blob}
</script>
<script>
{SCRIPT.strip()}
</script>
</body>
</html>
"""


def main(argv):
    stale = []
    for variant, out in OUTPUTS.items():
        text = build(variant)
        name = out.relative_to(ROOT).as_posix()
        current = out.read_text(encoding='utf-8').replace('\r\n', '\n') if out.is_file() else None
        if current == text:
            print(f'{name} is up to date')
        elif '--check' in argv:
            stale.append(name)
            print(f'{name} is out of date: run py -3 {SCRIPT_NAME}')
        else:
            out.write_text(text, encoding='utf-8', newline='\n')
            print(f'wrote {name} ({len(text.encode("utf-8")) // 1024} KiB)')
    nojekyll = ROOT / 'docs/.nojekyll'           # GitHub Pages: serve the files as they are
    if not nojekyll.is_file() and '--check' not in argv:
        nojekyll.write_bytes(b'')
    return 1 if stale else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))