<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# AI prompt examples (Claude, Codex and other assistants)

[Home](Home.md) › AI prompt examples

**Contents:** [Safety rules](#safety-rules-read-first) · [Connect an assistant](#connect-an-assistant-mcp) ·
[Prompts that only read](#prompts-that-only-read) · [Prompts that may change the console](#prompts-that-may-change-the-console) ·
[Prompts for developers](#prompts-for-developers) · [Bug reports](#prompts-for-bug-reports)

## Safety rules (read first)

1. **You approve every change.** The Manager's API lets an assistant *read* freely and *ask* for a job;
   every write waits for **your Approve** in the Manager's window. AI control is **off** unless you turn it
   on (Settings › AI control), and **Stop AI now** ends it at once.
2. **Never give an assistant your secrets.** Do not paste Wi-Fi passwords, Jellyfin passwords, tokens,
   server addresses or backups into a chat. The Manager never returns them through its API.
3. **Be present.** Only let an assistant start a console job when you are at the console, it is plugged in
   and on, and you can switch it off and on if asked.
4. **No flashing by hand.** Ask assistants to use Chromatic Manager only, never `esptool write_flash`,
   `idf.py flash` or `openFPGALoader --write-flash` directly. (The project makes `idf.py flash` fail on purpose.)
5. **Enough budget left.** AI-started writes stay **refused** unless you turn AI control on (rule 1), and even
   then each one also needs the [full backup](Install-with-Chromatic-Manager.md#full-backup-before-any-fpga-change)
   and a fresh *usage reading*, so an assistant that is about to run out of its limits does not start a
   firmware change it cannot finish. The assistant (or you) writes the reading to
   `%LOCALAPPDATA%\Jellymatic\chromatic-tools\usage-snapshot.json` just before it asks:

   ```json
   {"observed_at": "2026-11-10T14:02:00+00:00", "used_percent_by_window": [41, 12],
    "reporter": "claude", "ordinary_usage_allowed": true}
   ```

   - `observed_at`: when the usage was read, with its time zone; older than **5 minutes** is refused.
   - `used_percent_by_window`: percent **used** (0 to 100) of each usage limit the assistant has (for example
     its session and weekly limits). If any is at the reserve, the write is refused: **75%** used, or **90%**
     for `"reporter": "claude"`.
   - `reporter`: a short name of the assistant; `ordinary_usage_allowed`: must be `true`.

   Writes you start yourself in the window never need this file.
6. **Check what it says.** An assistant can be wrong. Read the Approve window yourself: it names the
   exact image that will be written.

## Connect an assistant (MCP)

Chromatic Manager has an MCP server (`scripts/chromatic_manager_mcp.py`). It attaches to the open window
through a local, token-protected API on `127.0.0.1`.

**Claude Code:**

```powershell
claude mcp add chromatic-manager -- "$env:LOCALAPPDATA\Jellymatic\venv\Scripts\python.exe" -B "C:\path\to\Jellymatic\scripts\chromatic_manager_mcp.py"
```

**Codex** (`~/.codex/config.toml`):

```toml
[mcp_servers.chromatic-manager]
# the full path of %LOCALAPPDATA%\Jellymatic\venv\Scripts\python.exe on your PC
command = 'C:\path\to\LocalAppData\Jellymatic\venv\Scripts\python.exe'
args = ['-B', 'C:\path\to\Jellymatic\scripts\chromatic_manager_mcp.py']
```

**Other MCP clients:** the same command and arguments, as a stdio server.

## Prompts that only read

> Use the chromatic-manager tools to check my Chromatic and tell me, in plain words, what is installed and
> whether anything needs doing. Do not start any job.

> List my Chromatic backups (paths only) and tell me whether both copies exist.

> Read the last Chromatic Manager job's log and explain what happened. Do not change anything.

## Prompts that may change the console

> My Chromatic is plugged in, on, and I am here. Ask Chromatic Manager to install the start-up menu. I will
> approve it in the window. Stop and tell me if it asks for anything else.

> Ask Chromatic Manager to run "Update the Pocket link". I will check a game on the console while the new
> image runs from RAM and approve it myself. If any automatic check fails, stop and explain.

> Something went wrong during an install. Ask Chromatic Manager to finish the interrupted job, and tell
> me what it says. Do not start anything else.

## Prompts for developers

> In this repository, run the host tests (scripts/test_chromatic_launcher.py, test_chromatic_setup.py,
> test_chromatic_manager.py) and summarise failures. Do not open any device or serial port.

> Explain how the start-up menu decides between the menu and the official firmware
> (chromatic/launcher/bootloader_components, shared/boot_decision.h). Do not change code.

> Add a test to scripts/test_chromatic_setup.py for <behaviour>. Keep the power-cut model passing.

## Prompts for bug reports

> Help me write a bug report for Jellymatic from this Chromatic Manager log. Remove every address, network
> name, user name, token and device ID before showing it to me.

Never paste logs that show addresses or names into a public issue. See [SECURITY.md](../SECURITY.md).
