# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms (author attribution, GPLv3 section 7(b)): see ADDITIONAL-TERMS.md
"""An independently supplied adapter can load and report through the public contract."""

from pathlib import Path
import tempfile
import threading
import time
import unittest

from plugin_runtime import PluginRuntime


class PluginRuntimeTest(unittest.TestCase):
    def test_local_adapter_with_helper_module(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            preset = root / "presets" / "sample"
            preset.mkdir(parents=True)
            (preset / "helper.py").write_text("NAME = 'sample'\n", encoding="utf-8")
            (preset / "adapter.py").write_text(
                "from .helper import NAME\n"
                "class Adapter:\n"
                "    def status(self):\n"
                "        return {'connected': True, 'session': True, 'host': '10.123.45.8', "
                "'discovery': 'paired', 'meta': {'deviceModel': 'basic'}, 'perf': None}\n"
                "    def request(self, kind, payload):\n"
                "        return f'{NAME}:{kind}:{payload}'\n"
                "def create_adapter(local_dir, manifest):\n"
                "    return Adapter()\n", encoding="utf-8")
            runtime = PluginRuntime({"id": "sample"}, root / "presets", root / "local")
            thread = threading.Thread(target=runtime.run_forever, daemon=True)
            thread.start()
            for _ in range(20):
                if runtime.status()["connected"]:
                    break
                time.sleep(.05)
            self.assertTrue(runtime.status()["connected"])
            self.assertEqual(runtime.request("console", "help"), "sample:console:help")
            self.assertTrue((root / "local" / "sample").is_dir())
            runtime.stop()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
