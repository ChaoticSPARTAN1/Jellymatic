# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms (author attribution, GPLv3 section 7(b)): see ADDITIONAL-TERMS.md
"""python-v1 entry point for the Chromatic preset. All logic lives in the chromatic package."""

from chromatic.runtime import ChromaticRuntime


def create_adapter(local_dir, manifest):
    return ChromaticRuntime(local_dir, manifest)
