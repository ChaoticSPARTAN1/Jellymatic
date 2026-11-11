# Contributing

Thank you for helping. Bug reports, bug fixes and pull requests are all welcome. This is a hobby
project, so there is **no guarantee of a response or a fix**, but every report is read when time
allows.

## Reporting a bug

Open an issue and include:

- what you did, what you expected and what happened instead;
- your Windows and Python versions (`py -3 --version`) and your browser;
- whether you used a real console (and its launcher version, from the viewer's facts list) or a
  simulator connection;
- the steps to reproduce it with a **Simulated** connection, if you can.

**Before you paste anything, remove private details.** Never post pairing keys, the contents of
`local\` (for example `local\chromatic\pairing.json` or `config.json`), Wi-Fi names or passwords,
server addresses, tokens, or logs that show addresses. Turn the viewer's privacy eye on (the default)
before taking screenshots. Security problems go to [SECURITY.md](SECURITY.md), not to a public issue.

## Pull requests

1. Keep changes small and focused, one topic per pull request.
2. Run the tests before you send it (see [docs/Building-from-source.md](docs/Building-from-source.md)):

   ```text
   py -3 -m unittest
   node test_chromatic_panel.js
   node test_privacy.js
   node test_tooltips.js
   node test_pointer_controls.js
   ```

3. Keep the shared viewer files (`viewer.html`, `viewer.js`, `viewer.css`, `themes.css`) free of
   any device or brand name; device specifics belong in `presets/<id>/` and their adapter.
4. Keep the safety rules: the viewer binds to 127.0.0.1 by default, every state-changing request
   needs the `X-Remote-Pilot-Viewer` header, the receiver never writes console flash itself, and no
   secret ever appears in a status field, a log line or a URL.
5. Do not add files you are not allowed to share (firmware images, other people's code without a
   compatible licence, real screenshots with personal details).

By sending a pull request you agree that your contribution is licensed under the project's licence,
GPL-3.0-or-later with the attribution term in [ADDITIONAL-TERMS.md](ADDITIONAL-TERMS.md).
