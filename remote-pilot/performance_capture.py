# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms (author attribution, GPLv3 section 7(b)): see ADDITIONAL-TERMS.md
"""Private per-preset opt-in for automatic performance sampling."""
import json
from pathlib import Path
import threading

_LOCK = threading.RLock()


def enabled(local_dir):
    with _LOCK:
        try:
            return json.loads((Path(local_dir) / "performance.json").read_text(encoding="utf-8")).get("enabled") is True
        except (OSError, ValueError, AttributeError):
            return False


def set_enabled(local_dir, value):
    if type(value) is not bool:
        raise ValueError("Performance capture needs a boolean")
    with _LOCK:
        folder = Path(local_dir)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "performance.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"enabled": value}) + "\n", encoding="utf-8")
        temporary.replace(path)
    return value
