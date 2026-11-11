<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# Recovery and troubleshooting

[Home](Home.md) › Recovery

**Contents:** [How Jellymatic avoids breaking Chromatic OS](#how-jellymatic-avoids-breaking-chromatic-os) ·
[If an install stops half-way](#if-an-install-stops-half-way) · [What the result means](#what-the-result-means) ·
[Reinstall the start-up menu](#reinstall-or-repair-the-start-up-menu) · [Turn the menu off or remove it](#turn-the-menu-off-or-remove-it) ·
[Put the previous FPGA image back](#put-the-previous-fpga-image-back) · [Restore the original FPGA image](#restore-the-original-fpga-image) ·
[Official updates and restoring Chromatic OS](#official-updates-and-restoring-chromatic-os) · [Common problems](#common-problems)

## How Jellymatic avoids breaking Chromatic OS

- **The official firmware stays.** The start-up menu is added next to it; the official firmware slot,
  your settings and radio calibration are never written by an install.
- **Games is always one press away.** Any restart that is not a power-on goes straight to the official
  firmware; holding a button while switching on skips the menu.
- **Backups first.** Nothing is written before a verified full backup of both chips exists (the Manager
  takes it by itself the first time), saved with a second copy. The second copy is usually on the same disk;
  put it on another disk or a USB stick for real safety ([Install](Install-with-Chromatic-Manager.md#full-backup-before-any-fpga-change)).
- **A journal.** Every step is written to disk before it happens, so an interrupted job is finished or
  undone the next time the Manager runs. Nothing else runs until then.
- **Read-back checks.** Every written region is read back from the chip and compared, then the whole flash.
- **You approve.** Every write needs your **Approve** in the Manager's window; FPGA updates are first run
  from the chip's RAM so you can try a game **on the console** before you approve the write.
- **A start check.** After writing, the console is restarted and watched: it must start and keep running.
- **The chip's own rescue mode.** The ESP32's download mode is in read-only ROM. No flash content can
  block it, so the Manager and ModRetro's official updater can always reach the console.

## If an install stops half-way

Unplugged cable, flat battery, closed window: plug the console back in, switch it **on**, open the
Manager. It sees the journal and offers **Finish job** (finish or undo, from the journal and the backup).
**Close job** is only for a job the Manager has confirmed is no longer needed. If the console shows nothing
at all, it still works: the Manager reaches it through the ROM download mode.

## What the result means

| Result | Meaning | What to do |
|---|---|---|
| **Done** (green) | Written, read back, console seen starting | Nothing |
| **Nothing was changed** (amber) | Refused before writing (for example a console it does not recognise) | Read the reason; nothing to undo |
| **Put right** | An interrupted job was finished or undone | Nothing |
| **Stopped: this needs you** (red) | It stopped safely and tells you the one thing to do | Follow the words on screen, then Finish job |
| **…not seen restarting** | The flash is right, but the console was not seen starting | Unplug, switch off and on, try a game |

## Reinstall or repair the start-up menu

**Fix a problem › Repair menu** writes the start-up menu again (same checks as Install). Use it when the
menu is missing, damaged or switched off.

## Turn the menu off or remove it

| Button | What it does | Your Wi-Fi/Jellyfin set-up |
|---|---|---|
| **Switch menu off** | Erases only the menu program (power-safe); the console boots straight to the official firmware | Kept, for when you Install again |
| **Remove menu** | Puts the official boot program and partition table back and erases the menu area | Erased; the console's Jellyfin sign-in is signed out on the server |
| **Restore original** | Rewrites the ESP32 from your newest verified backup of the official firmware (settings kept). The first of those backups is your original, taken only from the official firmware as it comes | Erased; sign-in signed out |

After **Remove menu** or **Restore original** the console is exactly the official layout. Install brings
the menu back at any time.

## Put the previous FPGA image back

After **Update the Pocket link**, **Put the previous image back** (one Approve) writes the sound image
18.40 again, read back byte for byte.

## Restore the original FPGA image

**Fix a problem › Restore FPGA** writes your **own** original FPGA image from your full backup, reads all
4 MiB back and compares them, then restarts the chip. Nothing from anyone else's console is ever used.
That original is taken once, by the first full backup, and only from ModRetro's own console chip image
(never from Jellymatic's sound or Pocket link image); it is never replaced afterwards.

If the Manager says your full backup **could not be checked in both copies**, connect the disk or USB stick
that holds the second copy and try again. It never takes a new original over an existing one. If the
first backup is refused because the console is **not on the official firmware as it comes**, put the
official firmware back with ModRetro's updater (below), then press **Back up**.

## Official updates and restoring Chromatic OS

Use **ModRetro's official update tool**, downloaded from ModRetro (modretro.com). Jellymatic never ships
it. The Manager guides you (Home › More › **Official update**, or text menu choice 6):

1. The Manager switches the start-up menu off (power-safe).
2. You run ModRetro's updater: **firmware update only**, not Cart Clinic. Keep the console plugged in and
   on. When it reports success, close it and press **Done** in the Manager.
3. The Manager checks what changed (the updater does not check its own ESP32 write), backs up the new
   firmware, and switches the start-up menu back on (**Install**).

Good to know:

- The official updater writes the FPGA first, then the whole ESP32 through the ROM download mode, so
  **it always works**, whatever Jellymatic left on the console. That is your last-resort restore of
  Chromatic OS: run it, and the console is fully official again.
- It **erases your console settings** (brightness, palette and so on); that is the official updater's
  behaviour, not Jellymatic's.
- It also replaces the FPGA image with the official one, so Jellyfin sound and the Pocket link are gone
  until you install them again. The start-up menu stays on the flash, switched off, until **Install**.
- If the updater fails: unplug USB, switch the console off and on, run it again. It needs the internet and
  may install the Gowin USB driver on Windows.

To reinstall Jellymatic on top afterwards: open the Manager, press **Install**, then (optional) **Set up
Jellyfin sound** and **Update the Pocket link** again.

## Common problems

| Problem | Try |
|---|---|
| Manager says "plug in" | Use a data cable, switch the console **on**, close other tools that use the port (ModRetro's updater, a serial monitor) |
| "Not the console the backups came from" | Connect your own console, the one the backups were made from |
| "Not a layout or firmware this tool knows" | Your console has firmware Jellymatic was not tested with. Nothing was written. Use the official updater first, or wait for a newer Jellymatic |
| Menu does not appear | Is a cartridge in? Are you holding a button at power-on? |
| Games will not start from the menu | Let go of all buttons; insert a cartridge |

See also the [FAQ](FAQ.md).
