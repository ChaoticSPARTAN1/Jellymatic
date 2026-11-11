# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""The documentation copies: Open-Documentation.html, docs/index.html and the PDF guide. PC only.

  current    Open-Documentation.html and docs/index.html (GitHub Pages copy) equal a fresh build from the
             .md files (scripts/build_docs_html.py); the PDF guide's receipt (docs/.guide-source.sha256)
             matches the pages and build scripts of today and the PDF's own bytes (scripts/build_docs_pdf.py)
  offline    a strict Content-Security-Policy; no <script src>, <link>, <iframe>, <base> or similar; every
             picture, srcset, font and CSS url() is a file in the repository (or inline data); the script
             has no fetch/XMLHttpRequest/WebSocket; http(s) addresses appear only as links people click
             (or as text), and every such link opens in a new tab without a referrer
  links      every page link and #anchor points at an embedded page and heading; the GitHub links to the
             sibling projects are there
  private    only README.md, docs/*.md and the four notices are embedded, all tracked by Git (when Git is
             available); no home-folder paths in the pages or the PDF; the PDF names only the project
No network, no device, standard library only (the PDF is checked, not printed again).
"""
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('build_docs_html', HERE / 'build_docs_html.py')
docs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(docs)
ROOT = docs.ROOT
PDF = ROOT / 'docs' / f'{docs.PROJECT}-Guide.pdf'
RECEIPT = ROOT / 'docs/.guide-source.sha256'
SIBLINGS = ('https://github.com/ChaoticSPARTAN1/PocketFin', 'https://github.com/ChaoticSPARTAN1/Jellymatic')
LOADS = {'src', 'srcset', 'poster', 'data', 'action', 'formaction', 'background', 'manifest', 'ping', 'xlink:href'}
FORBIDDEN_TAGS = {'link', 'iframe', 'frame', 'object', 'embed', 'base', 'form', 'audio', 'video', 'track', 'portal'}
PERSONAL = re.compile(rb'(?i)\b[A-Z]:\\\\{1,2}Users\\\\|/Users/|/home/|file:///')

failures = []


def check(ok, what):
    if not ok:
        failures.append(what)
    print(('ok   ' if ok else 'FAIL ') + what)


class Tags(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.tags, self.ids = [], set()
        self.feed(text)
        self.close()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.tags.append((tag, attrs))
        if attrs.get('id'):
            self.ids.add(attrs['id'])

    handle_startendtag = handle_starttag


def check_page(variant, out):
    base = out.parent
    name = out.relative_to(ROOT).as_posix()

    def local(url):
        return url.startswith('data:image/') or (base / url.split('#')[0].split('?')[0]).is_file()

    page = out.read_text(encoding='utf-8').replace('\r\n', '\n') if out.is_file() else ''
    check(page == docs.build(variant), f'{name} matches a fresh build from the Markdown (if not: py -3 {docs.SCRIPT_NAME})')
    blob = re.search(r'<script type="application/json" id="docs-data">\n(.*?)\n</script>', page, re.S)
    if not blob:
        check(False, f'{name}: page data found')
        return
    shell = page.replace(blob.group(0), '')
    outer = Tags(page)
    csp = [a.get('content') for t, a in outer.tags if t == 'meta' and (a.get('http-equiv') or '').lower() == 'content-security-policy']
    check(csp == [docs.CSP] and "default-src 'none'" in csp[0] and 'http' not in csp[0],
          f'{name}: a strict Content-Security-Policy (default-src none, no remote hosts)')
    check(not [t for t, a in outer.tags if t in FORBIDDEN_TAGS] and not [a for t, a in outer.tags if t == 'script' and 'src' in a]
          and not re.search(r'(?i)https?:', shell), f'{name}: no external scripts, stylesheets, frames, forms, <base> or addresses')
    code = re.findall(r'<script>(.*?)</script>', page, re.S)[-1]
    check(not re.search(r'\b(fetch|XMLHttpRequest|WebSocket|EventSource|sendBeacon|importScripts|import\s*\()', code),
          f'{name}: the page script makes no requests')
    style = ''.join(re.findall(r'<style>(.*?)</style>', page, re.S))
    urls = re.findall(r'url\(\s*["\']?([^"\')]+)', style)
    check('@import' not in style and all(not re.match(r'(?i)[a-z]+:|//', u) and local(u) for u in urls),
          f'{name}: CSS loads only local files ({len(urls)} fonts)')

    data = json.loads(blob.group(1))
    pages = {p['path']: p for g in data['groups'] for p in g['pages']}
    parsed = {path: Tags(p['html']) for path, p in pages.items()}
    bad, external, unsafe, pictures, broken = [], [], [], 0, []
    for path, doc in parsed.items():
        for tag, attrs in doc.tags:
            if tag in FORBIDDEN_TAGS or tag in ('script', 'style'):
                bad.append(f'{path}: <{tag}>')
            for key, value in attrs.items():
                if key.startswith('on') or (key == 'style' and value and 'url(' in value):
                    bad.append(f'{path}: {key}')
                if value and re.search(r'(?i)https?:|//', value) and not (tag == 'a' and key == 'href') and not value.startswith('data:'):
                    bad.append(f'{path}: <{tag} {key}>')
                if key in LOADS and value is not None:
                    for part in ([value] if value.startswith('data:') else value.split(',') if key == 'srcset' else [value]):
                        url = part.strip().split(' ')[0]
                        pictures += 1
                        if (re.match(r'(?i)[a-z][a-z0-9+.-]*:|//', url) and not url.startswith('data:image/')) or not local(url):
                            bad.append(f'{path}: {key}={url[:60]}')
            href = attrs.get('href') or ''
            if tag != 'a' or not href:
                continue
            if re.match(r'(?i)https?:', href):
                external.append(href)
                rel = attrs.get('rel') or ''
                if attrs.get('target') != '_blank' or 'noopener' not in rel or 'noreferrer' not in rel:
                    unsafe.append(f'{path}: {href}')
            elif href.startswith('#'):
                target, _, anchor = href[1:].partition(':')
                if target not in pages or (anchor and 'h-' + anchor.lower() not in parsed[target].ids):
                    broken.append(f'{path} -> {href}')
            elif not re.match(r'(?i)[a-z][a-z0-9+.-]*:', href) and not (base / href.split('#')[0]).exists():
                broken.append(f'{path} -> {href} (file)')
    check(not bad, f'{name}: every picture and srcset is a file in the repository or inline ({pictures} checked)'
          + ('' if not bad else ': ' + '; '.join(bad[:8])))
    check(not unsafe, f'{name}: {len(external)} web links open in a new tab without a referrer' + ('' if not unsafe else ': ' + '; '.join(unsafe[:5])))
    check(all(any(h.rstrip('/').startswith(s) for h in external) for s in SIBLINGS), f'{name}: links to the sibling projects on GitHub')
    check(not broken, f'{name}: every page link, #anchor and file link resolves' + ('' if not broken else ': ' + '; '.join(broken[:10])))
    allowed = all(p == 'README.md' or re.fullmatch(r'docs/[^/]+\.md', p) or p in docs.EXTRA for p in pages)
    check(set(pages) == set(docs.page_paths()) and allowed and 'README.md' in pages,
          f'{name}: {len(pages)} pages embedded: README.md, docs/*.md and the notices only')
    check(not PERSONAL.search(page.encode('utf-8').replace(b'C:\\\\path\\\\to', b'')), f'{name}: no home-folder or personal paths')
    return pages


def main():
    pages = {}
    for variant, out in docs.OUTPUTS.items():
        pages = check_page(variant, out) or pages
    check((ROOT / 'docs/.nojekyll').is_file(), 'docs/.nojekyll is there (GitHub Pages serves the files as they are)')

    receipt = dict(line.split(' ', 1) for line in RECEIPT.read_text(encoding='ascii').split('\n') if ' ' in line) if RECEIPT.is_file() else {}
    pdf = PDF.read_bytes() if PDF.is_file() else b''
    check(receipt.get('sources') == docs.source_hash(),
          f'{PDF.name} was printed from today\'s pages and build scripts (if not: py -3 {docs.SCRIPT_NAME.replace("html", "pdf")})')
    check(pdf.startswith(b'%PDF-') and receipt.get('pdf') == hashlib.sha256(pdf).hexdigest(), f'{PDF.name} is the file the receipt names')
    info = re.findall(rb'/Info\s+(\d+)\s+0\s+R', pdf)
    last = re.search(rb'\n' + info[-1] + rb' 0 obj\s*(<<.*?>>)', pdf, re.S).group(1) if info else b''
    check(b'/Author (ChaoticSPARTAN1)' in last and b'Chromium' not in last and b'Skia' not in last and b'Date' not in last,
          f'{PDF.name}: document information names only the project and ChaoticSPARTAN1')
    check(not PERSONAL.search(pdf), f'{PDF.name}: no home-folder paths or file links in its bytes')
    check(0 < len(pdf) < 15 * 1048576, f'{PDF.name} is {len(pdf) / 1048576:.1f} MiB (limit 15 MiB)')

    try:
        tracked = set(subprocess.run(['git', '-C', str(ROOT), 'ls-files'], capture_output=True, text=True,
                                     check=True, timeout=60).stdout.split('\n'))
    except (OSError, subprocess.SubprocessError):
        tracked = set()
    if len(tracked) > 1:
        check(all(p in tracked for p in pages), 'every embedded page is tracked by Git (nothing personal or untracked)')
    else:
        print('info no Git checkout: the tracked-file check is skipped')

    if failures:
        print(f'FAIL: {len(failures)} check(s) failed')
        return 1
    print(f'PASS: Open-Documentation.html, docs/index.html and {PDF.name} are current, offline and hold only the '
          f'{len(pages)} tracked documentation pages')
    return 0


if __name__ == '__main__':
    sys.exit(main())
