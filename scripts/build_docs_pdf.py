# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Builds the PDF guide in docs/: every documentation page, in reading order, in one themed PDF.

GitHub shows a PDF right in the browser, so the guide is readable from the repository page without
any website. It is printed from the same Markdown as Open-Documentation.html (build_docs_html.py), with
a clickable table of contents, a page break before each page and the same colours.

Needs Google Chrome or Microsoft Edge (used headless, offline, to print). Set DOCS_BROWSER to the
browser's program file if it is not found. Writes:
  docs/<project>-Guide.pdf     the guide (document information set to the project; no dates or paths)
  docs/.guide-source.sha256    what it was made from (pages + build scripts) and the PDF's own SHA-256;
                               the docs test uses it to say when the PDF is out of date

    py -3 scripts/build_docs_pdf.py

Run it after editing any documentation page (after build_docs_html.py).
"""
from __future__ import annotations

import hashlib
import html
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_docs_html as docs  # noqa: E402

ROOT = docs.ROOT
PDF = ROOT / 'docs' / f'{docs.PROJECT}-Guide.pdf'
RECEIPT = ROOT / 'docs/.guide-source.sha256'
COVER = {'Jellymatic': 'docs/images/readme/jellymatic-banner.png', 'PocketFin': 'docs/images/readme/pocketfin-banner-dark.png'}
BROWSERS = [
    r'%ProgramFiles%\Google\Chrome\Application\chrome.exe', r'%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe',
    r'%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe', r'%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe',
    r'%ProgramFiles%\Microsoft\Edge\Application\msedge.exe', 'google-chrome', 'chromium', 'chromium-browser',
    'microsoft-edge', '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
]

PRINT_BASE = """
@page { size:A4; margin:0; }
html { -webkit-print-color-adjust:exact; print-color-adjust:exact; }
body { margin:0; }
.sheet { break-before:page; -webkit-box-decoration-break:clone; box-decoration-break:clone; }
.sheet:first-child { break-before:auto; }
.md pre, .md pre code { white-space:pre-wrap; word-break:break-word; }
.md .table { overflow:visible; }
.md img, .md tr, .md pre, .md blockquote { break-inside:avoid; }
.md h1, .md h2, .md h3, .md h4 { break-after:avoid; }
.toc ol { list-style:none; padding:0; margin:0 0 18px; }
.toc li { display:flex; gap:10px; align-items:baseline; margin:6px 0; }
.toc li a { flex:none; }
.toc li .dots { flex:1; border-bottom:2px dotted currentColor; opacity:.35; }
.cover { text-align:center; }
.cover img { width:100%; height:auto; }
.cover { box-sizing:border-box; min-height:296mm; }
"""

PRINT_CHROMA = """
html { background-color:#1a0820;
  background-image:repeating-linear-gradient(135deg, #3a0a4f 0px, #5b1470 120px, #8a1838 240px, #c0263a 300px, #8a1838 360px, #5b1470 480px, #3a0a4f 600px); }
body.chroma { background:transparent; min-height:0; padding:0; }
.sheet { border:9mm solid transparent; background:#000; background-clip:padding-box; padding:9mm 10mm 10mm;
  box-shadow:inset 0 0 0 6px var(--accent), inset 0 0 0 10px #000, inset 0 0 0 13px #6e6e6e; }
.sheet > .titlebar { margin:0 0 16px; }
.sheet > .crumb { margin-bottom:16px; }
.cover .titlebar { margin-top:4px; }
.cover p { font-family:"PixelHead", monospace; font-size:12px; color:#ffd93c; line-height:2; }
.cover .credit { color:#cfcfcf; font-size:9px; }
.toc h2 { font-family:"PixelHead", monospace; font-weight:normal; font-size:12px; color:#000; text-transform:uppercase; margin:20px 0 8px; }
.toc a { font-family:"PixelHead", monospace; font-size:10px; color:#3cff3c; text-transform:uppercase; text-decoration:none; }
.toc .dots { color:#3cff3c; }
.toc .where { font-size:12px; color:#a8a8a8; }
"""

PRINT_MINIMAL = """
html { background:#000; }
body.minimal { padding:0; min-height:0; }
.sheet { border:14mm solid #000; border-left-width:16mm; border-right-width:16mm; background:#000; }
.sheet > .titlebar { margin-bottom:18px; }
.cover { padding-top:30mm; }
.cover p { color:#bdbdbd; font-size:15px; }
.toc h2 { color:#777; font-size:11px; letter-spacing:.12em; text-transform:uppercase; font-weight:normal; margin:20px 0 8px; }
.toc a { color:#f2f2f2; font-size:15px; text-decoration:none; }
.toc .where { color:#777; font-size:12px; }
"""


def relink(fragment, n, index):
    """Page links of the interactive page (#path:anchor) -> anchors inside the one print document."""
    def href(m):
        target, _, anchor = html.unescape(m.group(1)).partition(':')
        if target not in index:
            return m.group(0)
        return f'href="#p{index[target]}' + (f'-h-{html.escape(anchor.lower(), quote=True)}' if anchor else '') + '"'
    fragment = re.sub(r'href="#([^"]+)"', href, fragment)
    return fragment.replace(' id="h-', f' id="p{n}-h-')


def print_html():
    texts = docs.page_texts()
    paths = [path for path, _ in texts]
    index = {path: n for n, path in enumerate(paths, 1)}
    titles = {path: docs.label(path, text) for path, text in texts}
    chroma = docs.THEME == 'chroma'
    dither = '<div class="dither"></div>'
    bar = (lambda title: f'<header class="titlebar"><h1>{docs.esc(title)}</h1>{dither}</header>') if chroma else \
          (lambda title: f'<header class="titlebar"><h1>{docs.esc(title)}</h1><span class="brand">{docs.esc(docs.PROJECT)}</span></header>')
    toc = []
    for group, members in docs.grouped(paths):
        toc.append(f'<h2>{docs.esc(group)}</h2><ol>')
        toc += [f'<li><a href="#p{index[p]}">{docs.esc(titles[p])}</a><span class="dots"></span>'
                f'<span class="where">{docs.esc(p)}</span></li>' for p in members]
        toc.append('</ol>')
    sheets = [
        f'<section class="sheet cover">{bar(docs.PROJECT + " guide")}'
        f'<p><img src="{COVER[docs.PROJECT]}" alt="{docs.esc(docs.PROJECT)}"></p>'
        f'<p>Every page of the documentation, printed from the repository\'s Markdown.<br>'
        f'For the interactive version, download the repository and double-click Open-Documentation.html.</p>'
        f'<p class="credit">{"CREATED BY CHAOTICSPARTAN1" if chroma else "Created by ChaoticSPARTAN1"}</p></section>',
        f'<section class="sheet toc">{bar("Contents")}<div class="md">{"".join(toc)}</div></section>',
    ]
    for path, text in texts:
        body = relink(docs.markdown(text, path, paths, 'print'), index[path], index)
        sheets.append(f'<section class="sheet" id="p{index[path]}">{bar(titles[path])}'
                      f'<div class="crumb">{docs.esc(docs.PROJECT)} / {docs.esc(path)}</div><article class="md">{body}</article></section>')
    style = docs.STYLE_BASE + (docs.STYLE_CHROMA if chroma else docs.STYLE_MINIMAL) + PRINT_BASE + (PRINT_CHROMA if chroma else PRINT_MINIMAL)
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{docs.esc(docs.PROJECT)} Guide</title>'
            f'<style>{docs.font_faces()}{style}</style></head><body class="{docs.THEME}">' + '\n'.join(sheets) + '</body></html>')


def find_browser():
    for name in [os.environ.get('DOCS_BROWSER', '')] + BROWSERS:
        if not name:
            continue
        path = os.path.expandvars(name)
        if os.path.isfile(path):
            return path
        found = shutil.which(path)
        if found:
            return found
    raise SystemExit('Chrome or Edge not found: install one, or set DOCS_BROWSER to its program file')


def clean_info(pdf):
    """Replaces the document information (browser name, dates) with the project's own, as an update."""
    info = (f'<< /Title ({docs.PROJECT} Guide) /Author (ChaoticSPARTAN1) /Subject ({docs.TAGLINE}) '
            f'/Creator ({docs.PROJECT} build_docs_pdf.py) /Producer ({docs.PROJECT}) >>').encode('ascii')
    trailer_at = pdf.rfind(b'trailer')
    startxref = int(re.search(rb'startxref\s+(\d+)', pdf[pdf.rfind(b'startxref'):]).group(1))
    if trailer_at < 0:
        raise SystemExit('unexpected PDF layout (no classic trailer); the guide was not changed')
    trailer = pdf[trailer_at:]
    size = int(re.search(rb'/Size\s+(\d+)', trailer).group(1))
    root = re.search(rb'/Root\s+(\d+\s+\d+\s+R)', trailer).group(1)
    old_info = re.search(rb'/Info\s+(\d+)\s+(\d+)\s+R', trailer)
    # Blank the old information dictionary in place (same length, so offsets stay valid).
    if old_info:
        obj = re.search(rb'\n' + old_info.group(1) + rb' ' + old_info.group(2) + rb' obj\s*(<<.*?>>)\s*endobj', pdf, re.S)
        if obj:
            start, end = obj.span(1)
            pdf = pdf[:start] + b'<<' + b' ' * (end - start - 4) + b'>>' + pdf[end:]
    if not pdf.endswith(b'\n'):
        pdf += b'\n'
    offset = len(pdf)
    pdf += f'{size} 0 obj\n'.encode() + info + b'\nendobj\n'
    xref = len(pdf)
    pdf += f'xref\n{size} 1\n{offset:010d} 00000 n \ntrailer\n<< /Size {size + 1} /Root '.encode() + root
    pdf += f' /Info {size} 0 R /Prev {startxref} >>\nstartxref\n{xref}\n%%EOF\n'.encode()
    return pdf


def main():
    browser = find_browser()
    page = ROOT / '.guide-print.html'           # next to docs/, so pictures and fonts load by relative path
    work = Path(tempfile.mkdtemp(prefix='guide-'))
    try:
        page.write_text(print_html(), encoding='utf-8')
        out = work / 'guide.pdf'
        run = subprocess.run([browser, '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
                              f'--user-data-dir={work / "profile"}', '--disable-extensions', '--disable-background-networking',
                              '--no-pdf-header-footer', '--generate-pdf-document-outline', '--virtual-time-budget=15000',
                              f'--print-to-pdf={out}', page.as_uri()], capture_output=True, timeout=300)
        if not out.is_file() or out.stat().st_size < 1000:
            raise SystemExit('printing failed:\n' + run.stderr.decode('utf-8', 'replace')[-2000:])
        pdf = clean_info(out.read_bytes())
    finally:
        page.unlink(missing_ok=True)
        shutil.rmtree(work, ignore_errors=True)
    for needle in (str(ROOT).encode(), str(Path.home()).encode(), b'file:///', b'(file:', b'\\Users\\', b'/Users/'):
        if needle in pdf:
            raise SystemExit(f'the PDF contains a local path ({needle[:20]!r}); not written')
    for stream in re.findall(rb'stream\r?\n(.*?)\r?\nendstream', pdf, re.S):
        try:
            text = zlib.decompress(stream)
        except zlib.error:
            continue
        if b'file:///' in text or str(ROOT).encode() in text:
            raise SystemExit('the PDF contains a local file link; not written')
    PDF.write_bytes(pdf)
    RECEIPT.write_text(f'sources {docs.source_hash()}\npdf {hashlib.sha256(pdf).hexdigest()}\n', encoding='ascii', newline='\n')
    print(f'wrote {PDF.relative_to(ROOT).as_posix()} ({len(pdf) / 1048576:.1f} MiB) and {RECEIPT.relative_to(ROOT).as_posix()}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
