<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# Console chip (FPGA) images

Each folder holds the **exact sources and build receipts** of one reviewed image, built from ModRetro's
open-source Chromatic FPGA design v18.14 (GPL-3.0; commit in `../upstream.lock.json`) plus Jellymatic's
changes. Only the files that differ from ModRetro's tree are here; the receipts list every one with its
SHA-256.

| Folder | Image | What it adds |
|---|---|---|
| `images/sound-18.40/` | 18.40 | Sound hooks: the start-up menu's Jellyfin app can play sound; games unchanged |
| `images/link-18.41/` | 18.41 | 18.40 + the Pocket link bridge (link port ↔ start-up menu) |

The bitstreams (`build/evt1_x2.fs`, `build/evt1_x2.bin`) are **not** in Git. Jellyfin sound and the Pocket link
need the **official release files**, published separately as release assets: download
`Jellymatic-FPGA-images-<version>.zip` from https://github.com/ChaoticSPARTAN1/Jellymatic/releases/latest and
extract it into the Jellymatic folder, keeping its folders (it puts them in each folder's `build/`).
Chromatic Manager refuses an image that does not match its receipt, and for 18.41 also the SHA-256 pins in
`scripts/chromatic_fpga.py`, so a build of your own will not match and is refused
([docs/Building-from-source.md](../docs/Building-from-source.md#console-chip-fpga-images)).

ModRetro's files here are modified by ChaoticSPARTAN1 for Jellymatic, 2026-11 (see `../NOTICE`).

Licences: ModRetro's files and the MiSTer core (Gameboy_MiSTer) files keep their GPL notices; Jellymatic's files
(`chromatic_media.v`, `pocket_link/*`) are GPL-3.0-or-later. A built image also contains parts under other
terms (T80 BSD-style, SameBoy boot ROM MIT, Gowin's encrypted IP): see `../THIRD-PARTY-NOTICES.md` and the
licence texts in `../LICENSES/`.
