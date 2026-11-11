# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms (author attribution, GPLv3 section 7(b)): see ADDITIONAL-TERMS.md
"""Shared capture bundles for any transport: screenshots plus available diagnostics."""

import json
from pathlib import Path
import re
import time


def schema(db):
    db.execute("CREATE TABLE IF NOT EXISTS captures (id INTEGER PRIMARY KEY, preset TEXT, created REAL)")
    db.execute("CREATE TABLE IF NOT EXISTS capture_items (capture INTEGER, task INTEGER UNIQUE, "
               "kind TEXT, file TEXT DEFAULT '', PRIMARY KEY(capture, kind))")


def create(db, preset, diagnostic, plugin=False):
    """Reserve the whole bundle atomically, before dispatching either task."""
    schema(db)
    kinds = ['shot'] + ([diagnostic] if diagnostic in ('log', 'report') else [])
    pending = db.execute("SELECT count(*) FROM tasks WHERE state IN ('queued', 'sent')").fetchone()[0]
    if pending + len(kinds) > 16:
        raise ValueError('Queue full; wait for the device')
    capture = db.execute('INSERT INTO captures (preset, created) VALUES (?, ?)',
                         (preset, time.time())).lastrowid
    items = []
    for kind in kinds:
        wire_kind = f'plugin:{preset}:{kind}' if plugin else kind
        task = db.execute("INSERT INTO tasks (kind, payload, state) VALUES (?, '-', ?)",
                          (wire_kind, 'sent' if plugin else 'queued')).lastrowid
        db.execute('INSERT INTO capture_items (capture, task, kind) VALUES (?, ?, ?)',
                   (capture, task, kind))
        items.append((task, kind))
    return capture, items


def record_file(db, task, name):
    if not task:
        return
    schema(db)
    if Path(name).name != name or not re.fullmatch(r'[a-zA-Z0-9_.-]+', name):
        raise ValueError('Invalid capture artifact name')
    db.execute('UPDATE capture_items SET file=? WHERE task=?', (name, task))


def list_bundles(db, preset=None):
    schema(db)
    rows = db.execute('SELECT id, preset, created FROM captures ' +
                      ('WHERE preset=? ' if preset else '') + 'ORDER BY id DESC LIMIT 30',
                      (preset,) if preset else ()).fetchall()
    result = []
    for ident, game, created in rows:
        items = [dict(task=task, kind=kind, state=state, file=name or None, result=message)
                 for task, kind, state, name, message in db.execute(
                     'SELECT i.task, i.kind, t.state, i.file, t.result FROM capture_items i '
                     'JOIN tasks t ON t.id=i.task WHERE i.capture=? ORDER BY i.task', (ident,))]
        result.append(dict(id=ident, preset=game, created=created, items=items))
    return result


def save_manifests(db, folder):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    for bundle in list_bundles(db):
        path = folder / f"capture-{bundle['id']}.json"
        data = json.dumps(bundle, indent=2) + '\n'
        if not path.exists() or path.read_text(encoding='utf-8') != data:
            # Callers hold the SQLite write transaction, serializing writers.
            temporary = path.with_suffix('.tmp')
            temporary.write_text(data, encoding='utf-8')
            temporary.replace(path)
