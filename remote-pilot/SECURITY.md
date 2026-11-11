# Security policy

## Reporting a vulnerability

Please report security problems **privately**, not in a public issue: use GitHub's
**Security → Report a vulnerability** button on this repository (private vulnerability reporting).
Describe the problem, the version or commit, and how to reproduce it, ideally with a **Simulated**
connection. You will get an answer when time allows; this is a hobby project with no guaranteed
response time.

## Never post publicly

- pairing keys or anything from the `local\` folder (`local\chromatic\pairing.json`,
  `config.json`, captures, logs);
- Wi-Fi network names or passwords;
- server addresses, IP addresses, host names or tokens;
- logs, screenshots or capture files that show any of the above (keep the viewer's privacy eye on,
  which is the default, before taking screenshots).

## What Remote Pilot is designed to protect

See [docs/Security-model.md](docs/Security-model.md) for the full description. In short:

- the viewer listens on 127.0.0.1 (this PC only) unless you start it with `--viewer-lan`; that LAN
  mode is plain HTTP (not encrypted) for a trusted home network only, with one-time pairing codes
  that expire after 10 minutes and are replaced after 20 wrong tries;
- other web sites cannot send it commands (numeric Host and same-Origin checks, a required custom
  header, no CORS);
- the Chromatic link uses a paired key and HMAC challenges, on private network addresses only;
- firmware changes are never made by Remote Pilot itself; it can only start the separate Chromatic
  Manager from this PC's own browser, after you switch it on in `local\chromatic\config.json`
  (`"managerRunAllowed": true`) and type an exact confirmation phrase.

Keep the Remote Pilot folder out of OneDrive and other cloud-synced folders: `local\` holds the
pairing key.

**Never expose Remote Pilot to the internet** (no port forwarding, no public tunnels or reverse
proxies). It is a local tool.
