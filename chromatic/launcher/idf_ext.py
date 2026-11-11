# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Refuses every idf.py command that writes or erases the Chromatic's flash.

idf.py merges this file's actions over its own, so these replace the real commands.
erase-flash would wipe all 4 MiB (stock firmware and the owner's settings), and every
flash target would write this app over the stock firmware at 0x10000. Install only
with scripts/chromatic_manager.py, which is journalled and protects both.
"""

REFUSED = ('erase-flash', 'erase_flash', 'flash', 'app-flash', 'app_flash', 'bootloader-flash',
           'partition-table-flash', 'encrypted-flash', 'encrypted-app-flash', 'erase-otadata',
           'erase_otadata', 'merge-bin', 'efuse-burn', 'efuse-burn-key', 'efuse-write-protect',
           'efuse-read-protect')


def action_extensions(base_actions, project_path):
    def refuse(action, ctx, args, **kwargs):
        raise SystemExit(f'REFUSED: idf.py {action} is disabled for the Chromatic launcher. '
                         'Use scripts/chromatic_manager.py; it protects stock firmware and settings.')

    return {'actions': {name: {'callback': refuse, 'help': 'Disabled: use scripts/chromatic_manager.py.',
                               'options': [], 'order_dependencies': []} for name in REFUSED}}
