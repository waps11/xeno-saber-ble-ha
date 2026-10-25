# Security policy

## Reporting a vulnerability

Please report vulnerabilities **privately**, through GitHub's
[private vulnerability reporting](https://github.com/waps11/xeno-saber-ble-ha/security/advisories/new)
(*Security* tab → *Report a vulnerability*). Do not open a public issue.

Include the version of the integration and of Home Assistant, what an attacker
needs (network position, Home Assistant account, physical access), and the
steps or a proof of concept. You will get an answer within a week; a fix is
released, and the advisory published with credit if you wish, once users can
update.

## Supported versions

Only the latest release receives security fixes.

## Scope

In scope, this integration's code, for example:

- an action reachable without the rights it should need (actions that touch
  files on the Home Assistant host or the saber's storage are reserved to
  administrators);
- reading or writing files outside the directories Home Assistant allows
  (`allowlist_external_dirs`, `/media`), including through a crafted `.zip`;
- secrets or identifiers left in the diagnostics file;
- the WLED UDP listener accepting packets it should filter out.

Out of scope: weaknesses of the saber and its Bluetooth protocol, which this
integration does not create and cannot fix. They are documented in
[PROTOCOL.md](PROTOCOL.md): the static password shared by every saber, the
lack of real authentication or encryption over Bluetooth, unsigned firmware
updates, and the fixed key of the custom button mapping file. Anyone within
Bluetooth range can control a saber, with or without this integration. Report
those to the manufacturer.
