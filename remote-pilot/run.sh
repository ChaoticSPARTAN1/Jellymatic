#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms (author attribution, GPLv3 section 7(b)): see ADDITIONAL-TERMS.md
cd "$(dirname "$0")" || exit 1
exec python3 receiver.py serve "$@"
