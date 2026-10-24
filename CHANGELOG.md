# Changelog

All notable changes to Xeno Saber BLE are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[semantic versioning](https://semver.org/).

## [0.1.0] - 2026-10-23

First release: an unofficial, fully local Home Assistant integration for
lightsabers with a Xenopixel V3 board, over Bluetooth Low Energy through the
Home Assistant host's adapter or an ESPHome Bluetooth proxy.

### Added

- **Blade** as a light (on/off, RGB colour, brightness); sound font and blade
  effect selectors; volume and blade length.
- **Effects**: clash, blaster and force buttons; lockup and drag switches;
  background music and motion control switches.
- **Settings**: every option of the Xeno Configurator app's settings screens,
  with the app's ranges: blade brightness, ignition and retraction times,
  power-on/off hold times, auto deep sleep, pre-on time, gesture sensitivities,
  side blade, crystal chamber motor, a light effect per event (blaster, force,
  lockup, clash, post-off), blade mode, blade modes and gestures, colour
  cycling, flash on clash, countdown and LED screen. *Save settings* and
  *Restart* buttons.
- **Sensors**: battery, font name, board configuration (blade firmware,
  hardware type, number of buttons) and file transfer progress; *Connected* and
  *Authorized* diagnostics and a *Reconnect* button.
- **Sound fonts**: `upload_sound_font` (from a folder or a `.zip`; Xeno, Proffie
  and other naming schemes are renumbered to the saber's), `export_sound_font`,
  `copy_sound_font`, `rename_sound_font`, `list_sound_fonts` (with free space),
  `list_sound_files` and `play_sound`. Uploads check the free space first, can
  delete the old font first, and create the next font folder like the app.
- **Files on the saber**: `upload_file`, `download_file`, `copy_file` and
  `delete_file` (settings files need confirmation).
- **Backup and restore**: `backup_saber` saves every font, `config.ini`, the
  custom button mapping, the system sounds and the current settings into one
  `.zip` laid out like a Xeno SD card; `restore_backup` writes it back, with a
  dry run, a free-space check and confirmation. Sabers without an SD card are
  supported: their fonts live in internal memory.
- **Firmware and board**: `update_firmware`, `set_blade_firmware` (Pixel/RGB)
  and `set_hardware_type`, each behind a confirmation.
- **Colour sync**: the blade follows a Home Assistant light and/or WLED UDP
  broadcasts, with an optional source-address filter.
- **Connection**: persistent or on-demand (so the phone app can still be used);
  keepalive, off by default, with a battery warning. A sleeping saber is not an
  error: its entities are unavailable until it wakes up and advertises. Several
  sabers can share one adapter: connection attempts take turns and an
  unreachable saber is retried with a growing delay.
- **Setup**: Bluetooth discovery and a connection test. Each saber has one name:
  the entry title and the device name follow each other, and new sabers are
  named after the end of their Bluetooth address, since every board advertises
  as *Saber*.
- A warning when the saber reports a firmware version that has not been tested,
  and a diagnostics download with the address redacted.
- Icon and logo with dark-theme variants (Home Assistant 2026.3+), and a user
  interface in 12 languages.
- PROTOCOL.md: the saber's BLE protocol, file system, file transfer, board
  configuration and custom button and gesture mapping file.

[0.1.0]: https://github.com/waps11/xeno-saber-ble-ha/releases/tag/v0.1.0
