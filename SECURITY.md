# Security policy

Cortex keeps a student's private notes, study history and browsing on their own computer, so a security bug is usually a privacy bug.

## Reporting a vulnerability

Report it privately through GitHub's private vulnerability reporting: open the repository's **Security** tab and click **Report a vulnerability**. Only the maintainers see the report until a fix is out.

**Never open a public issue for a security or privacy bug**, and don't post an exploit, a crafted note or a real vault anywhere public. A public report shows everyone how to read other people's data before they can update.

Please include:
- the Cortex version (`cortex --version`, or the installer's version) and your operating system;
- what an attacker can read, write or run, and from where (a crafted note, an imported chat, a web page, another local program);
- the smallest steps that show it, using a made-up vault (`cortex init <folder>`), never your own.

## What counts

Anything that breaks a promise in [docs/PRIVACY.md](docs/PRIVACY.md), for example:
- a private note, private browse page or hidden category reaching Claude, another AI app, the dashboard, search or a cloud model;
- note text running as code in the dashboard or the app, or being read as instructions outside `<untrusted_note>`;
- the app accepting a request from a web page or another computer;
- a secret surviving the redaction on import or at session close.

Bugs in Claude, Obsidian, your browser or a model runner belong to those projects.
