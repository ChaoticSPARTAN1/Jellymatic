# Third-party notices

Remote Pilot's own code is GPL-3.0-or-later (see [LICENSE](LICENSE)). These third-party parts are
included or used, each under its own licence.

## Bundled in this repository

| Component | Files | Licence | Licence text |
| --- | --- | --- | --- |
| Press Start 2P font, by CodeMan38 (Copyright 2012 The Press Start 2P Project Authors), Reserved Font Name "Press Start 2P" | `fonts/heading-PressStart2P-Regular.ttf` | SIL Open Font License 1.1 | [fonts/OFL-PressStart2P.txt](fonts/OFL-PressStart2P.txt) |
| Silkscreen font, by The Silkscreen Project Authors | `fonts/body-Silkscreen-Regular.ttf` | SIL Open Font License 1.1 | [fonts/OFL-Silkscreen.txt](fonts/OFL-Silkscreen.txt) |

Both fonts are unmodified copies from the Google Fonts repository (`ofl/pressstart2p`,
`ofl/silkscreen`); only the file names carry a `heading-` / `body-` prefix. The OFL allows them to be
bundled and redistributed with software as long as the licence text stays with them; they are not
sold on their own. See [fonts/LICENCES.txt](fonts/LICENCES.txt).

## Installed by the setup script (not included here)

| Component | Version | Licence | Used for |
| --- | --- | --- | --- |
| [pyserial](https://github.com/pyserial/pyserial) | 3.5 | BSD-3-Clause | USB serial link to the Chromatic (optional; Wi-Fi and the simulators work without it) |

`Setup-RemotePilot.ps1` installs it from PyPI into the private `local\venv` folder on your PC. It is
not part of this repository.

## Not used

Remote Pilot loads no JavaScript libraries, no web fonts from the internet and no CDN files: the
viewer is plain HTML, CSS and JavaScript served from this folder. The receiver uses only the Python
standard library (plus the optional pyserial above).
