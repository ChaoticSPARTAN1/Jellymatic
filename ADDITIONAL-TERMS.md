# Additional terms (GNU GPL version 3, section 7)

Jellymatic is licensed under the GNU General Public License, version 3 or (at your option) any later
version ([LICENSE](LICENSE)). These additional terms apply only to the material in this repository whose
copyright is held by its author, **ChaoticSPARTAN1**. They do not apply to parts made by others (see
[NOTICE](NOTICE) and [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md)), which keep their own licences, nor to
translations of others' files such as `chromatic/fpga-media/sim/speedcontrol_model.v` (GPL-3.0, as its original).

## 1. Attribution (section 7(b))

As permitted by GPLv3 section 7(b), if you convey this work or a modified version of it, you must
preserve the reasonable author attribution "ChaoticSPARTAN1":

1. in the credits section of the README (or the equivalent main documentation page), and
2. in any About, credits or similar screen that the program or its user interface shows (for example the
   start-up menu's credits screen and Chromatic Manager's About tab).

## 2. Marking modified versions (section 7(c))

As permitted by GPLv3 section 7(c), a modified version must be marked in reasonable ways as different
from the original version (for example in its name, version string or About/credits screen), and must not
be presented as the original Jellymatic or as made or endorsed by its author.

## 3. Additional permission for FPGA bitstreams (section 7, additional permission)

If you modify this Program, or any covered work, by combining or linking it with ModRetro's Chromatic
FPGA design (or a modified version of it) or with Gowin Semiconductor FPGA IP or primitives generated or
supplied by Gowin's FPGA design software (or modified versions of them), the licensors of this Program
grant you additional permission to convey the resulting work, including FPGA bitstreams. Corresponding
Source for a non-source form of such a combination need not include the source code of those parts,
which remain under their own licences.

This permission is given only for the copyright held by ChaoticSPARTAN1 (the RTL in `rtl/` and
`chromatic/fpga-media/`, and the build scripts). It cannot and does not change the licence of ModRetro's
design, which is itself GPL-3.0: anyone conveying a bitstream must still meet ModRetro's licence terms
and Gowin's terms for its IP. See THIRD-PARTY-NOTICES.md.

## 4. Additional permission for Espressif's binary-only libraries and the ESP32 ROM (section 7, additional permission)

If you modify this Program, or any covered work, by linking or combining it with the binary-only
libraries that Espressif Systems supplies with ESP-IDF for the ESP32 (such as the Wi-Fi, PHY, Bluetooth,
coexistence and RF calibration libraries, for example `libnet80211`, `libpp`, `libphy`, `librtc` and
`libcoexist`, or modified versions of them that Espressif supplies), or with the code in the ESP32 chip's
mask ROM, the licensors of this Program grant you additional permission to convey the resulting work.
Corresponding Source for a non-source form of such a combination need not include the source code of
those libraries or of the ROM, which remain under Espressif's own licence terms; the Corresponding Source
of the parts covered by this licence must still be provided.

This permission is given only for the copyright held by ChaoticSPARTAN1 (the start-up menu, the Jellyfin
app and the other code in `chromatic/`, `shared/` and `pocket/` that is built into the console's ESP32
firmware). It does not change the licence of any other part.

You may remove these additional permissions from your own copy or modified version, as GPLv3 section 7
allows. They do not grant any permission to use a name or trademark beyond the attribution above.
