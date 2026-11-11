# Install

[Home](Home.md) › Install

**On this page:** [What you need](#what-you-need) · [Step by step](#step-by-step) ·
[What the setup script does](#what-the-setup-script-does) · [Starting it](#starting-it) ·
[Updating and removing](#updating-and-removing)

## What you need

- A Windows 10 or 11 PC. (Linux and macOS work too: use `run.sh`; USB needs `pip install pyserial`.)
- [Python](https://www.python.org/downloads/) **3.11 or newer**. During its install, tick
  *Add python.exe to PATH* (or keep the `py` launcher, which is installed by default).
- A web browser (Edge, Chrome or Firefox).
- **No admin rights** are needed.

## Step by step

1. Download this repository: green **Code** button → **Download ZIP**, then unzip it somewhere you
   can write to that is **not synced to the cloud**, for example `C:\RemotePilot`.

   > **Keep it out of OneDrive, Dropbox, Google Drive and other synced folders.** On many PCs
   > Desktop and Documents are synced to OneDrive. The `local\` folder will hold
   > `pairing.json`, the key that lets this PC control your console; a synced folder would copy
   > it to the cloud.
2. Open the folder, right-click **`Setup-RemotePilot.ps1`** → **Run with PowerShell**.
   If Windows blocks scripts, open PowerShell in the folder and run:

   ```powershell
   powershell -ExecutionPolicy Bypass -File Setup-RemotePilot.ps1
   ```

3. When it says **Done**, double-click **`Start-RemotePilot.cmd`**. A black window opens; leave it
   open while you use Remote Pilot.
4. Open <http://127.0.0.1:38764/> in your browser.

## What the setup script does

It only:

1. checks that Python 3.11+ is installed;
2. creates a private Python environment in `local\venv` inside the Remote Pilot folder;
3. installs **pyserial 3.5** (for the USB cable link) into it from PyPI.

It does not need a password, key or token, changes no system settings, opens no USB device and
uploads nothing. You can run it again at any time. If you only want Wi-Fi and the simulators, run it
with `-SkipSerial`.

## Starting it

- `Start-RemotePilot.cmd` starts the receiver on **this PC only** (127.0.0.1, port 38764).
- Use another port with `Start-RemotePilot.cmd --viewer-port 38800`.
- Stop it by closing the black window or pressing **Ctrl+C** in it.

Use exactly `http://127.0.0.1:38764/`. The address `http://localhost:38764/` is refused on
purpose (see [Security model](Security-model.md)).

## Updating and removing

- **Update:** download the new version and copy your old `local\` folder into it (it holds your
  pairing key and settings), then run the setup script again.
- **Remove:** delete the folder. Nothing else was installed on your PC. (Delete `local\` too if
  you want the pairing key gone.)

Next: [Pairing with a Chromatic](Pairing-with-a-Chromatic.md)
