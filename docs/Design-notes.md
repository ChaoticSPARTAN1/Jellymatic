<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# Design notes (for developers)

[Home](Home.md) › Design notes

Some comments point to "docs/Design-notes.md", this page. A few, in the reviewed FPGA sources
(`fpga/images/`, the pinned files in `rtl/` and `chromatic/fpga-media/`) and in the staging scripts whose
text becomes part of those sources, still name the original internal design notes (`CHROMATIC-...md`).
Those notes are not published, and the pinned sources cannot be edited without changing their SHA-256
hashes. This page says where each topic lives now.

What they covered, and where to look instead:

| Note | Topic | Where now |
|---|---|---|
| POCKET-TETHER | Chromatic ↔ Pocket wire contract, credits, memory budget, link stages | `shared/pocket_tether.h`, `chromatic/launcher/shared/pocket_link.h`, `rtl/chromatic_pocket_link.v` and their tests; [Pocket link](Pocket-link.md) |
| REMOTE-CONTROL | CHR1 protocol (Remote Pilot ↔ launcher), USB pacing, pairing | `chromatic/launcher/shared/remote.h`, `remote-pilot/presets/chromatic/PROTOCOL.md`; [Remote Pilot](Remote-Pilot.md) |
| AUDIO-PLAN | FPGA sound hooks, version register, the FPGA job runbook | `chromatic/fpga-media/chromatic_media.v`, `scripts/stage_chromatic_fpga_media.py`, `scripts/chromatic_fpga.py`; [Install](Install-with-Chromatic-Manager.md#jellyfin-sound-fpga-1840), [Recovery](Recovery.md) |
| SETUP-ON-CONSOLE | Wi-Fi and Jellyfin screens, the one audited flash writer, power-cut model | `chromatic/launcher/shared/setup_ui.h`, `provision_write.c`, `scripts/test_chromatic_setup.py`; [First run](First-run-on-the-console.md) |
| JELLYFIN-PLAYER | The console's Jellyfin app, pacing, memory | `chromatic/jellyfin/jf_app.h`, `jf_ui.h`; [Using Jellyfin](Using-Jellyfin-on-the-Chromatic.md) |
| FPGA-STREAM-IMAGE | How the 18.41 image met timing, its pins | `fpga/images/link-18.41/` receipts, `scripts/stage_chromatic_fpga_link.py` |
| DIAGNOSTICS | The launcher's `PJ-DIAG` serial lines | `chromatic/launcher/shared/launcher_diag.h` |
| MANAGER | Chromatic Manager's behaviour in detail | `scripts/chromatic_manager*.py` docstrings and tests; [Install](Install-with-Chromatic-Manager.md) |

The provisioning record (Wi-Fi, pairing, Jellyfin sign-in in two flash sectors) is defined by
`scripts/chromatic_provision.py` and its C twin `chromatic/launcher/shared/provision.h`, cross-checked by
`scripts/test_chromatic_provision_c.py`.

## Start-up menu 1.6: TV, ChromaTube, Local Weather

| Note | Topic | Where now |
|---|---|---|
| LAUNCHER-1.6 | The five-row menu; TV and ChromaTube as modes of the Jellyfin app; live-channel tuning and closing; pacing | `chromatic/launcher/shared/launcher_logic.h`, `chromatic/jellyfin/jf_app.h`, `scripts/test_chromatic_tv.py`; [Apps on the start-up menu](Apps-on-the-start-up-menu.md) |
| WEATHER-CHANNEL | The Local Weather pages, data, radar decoder, clock, memory, sound; the weather location record (two 4 KiB sectors at 0x3FC000 and 0x3FD000, written A then B with sequence and CRC; the apps record and the Manager's launcher area lie below them); no record means the built-in default, Orlando, FL | `chromatic/weather/` (`wx_place.h` for the record), `scripts/chromatic_weather_location.py`, `scripts/test_chromatic_weather*.py`; [TV and Local Weather](TV-and-Local-Weather.md) |
| APPS (1.6.4) | Which app rows show, chosen in the Manager (Settings › Apps) or on the console (Select hides, Other firmware › Show hidden apps). A 32-byte versioned record with CRC ("PJAP" v1: sequence, flags TV / ChromaTube / Live TV) in two 4 KiB sectors at 0x3FA000 and 0x3FB000; absent or damaged means the defaults (TV with Local Weather only, ChromaTube hidden). The console's Hide/Show writes the copy not in force through the one audited writer. The Manager's launcher area ends at 0x3FA000 (largest image 1,679,360 bytes) | `chromatic/launcher/shared/apps_record.h`, `chromatic/launcher/shared/provision_write.c`, `scripts/chromatic_apps.py`, `scripts/test_chromatic_apps.py`; [Apps on the start-up menu](Apps-on-the-start-up-menu.md) |
| SERVER-APPS (1.6.4) | The Manager's Live TV channels and ChromaTube server set-ups | `scripts/chromatic_server_apps.py`, `scripts/test_chromatic_server_apps.py`; [TV and Local Weather](TV-and-Local-Weather.md#live-tv-optional), [ChromaTube](ChromaTube.md#set-it-up) |

The server sides of TV and ChromaTube are separate projects with their own tools and tests:
[Chromavision](https://github.com/ChaoticSPARTAN1/Chromavision) and [ChromaTube](https://github.com/ChaoticSPARTAN1/ChromaTube).
