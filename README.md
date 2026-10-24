<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="custom_components/xeno_saber_ble/brand/dark_logo.png">
    <img alt="Xeno Saber BLE" src="custom_components/xeno_saber_ble/brand/logo.png" width="420">
  </picture>
</p>

# Xeno Saber BLE

**Unofficial** Home Assistant integration for sabers fitted with a
**Xenopixel V3** compatible board (the ones configured with the
*Xeno Configurator* app). The protocol was documented for interoperability
purposes; details are in [PROTOCOL.md](PROTOCOL.md).

Fully local Bluetooth LE communication. Works with the HA host's Bluetooth
adapter or through **ESPHome Bluetooth proxies**.

> [!WARNING]
> **A newer saber firmware may break this integration.** The protocol is
> undocumented and was worked out from the official app; the manufacturer can
> change it in any firmware update, without notice. Only firmware
> `DMN_XENO_B_SV1.4.0` has been tested. If your saber works with this
> integration, think twice before updating its firmware through the Xeno
> Configurator app or your reseller. Home Assistant logs a warning when a saber
> reports a firmware version that has not been tested.

The integration's UI is translated into English, French, German, Spanish,
Italian, Dutch, Portuguese (Portugal and Brazil), Polish, Swedish, Japanese and
Simplified Chinese. Entity names follow your Home Assistant language.

## Supported devices

Sabers fitted with a **Xenopixel V3** board, the kind configured with the
*Xeno Configurator* app (sold under many brands, for example Xsaber, LGT and
Damiensaber resellers). The board, not the blade, speaks Bluetooth, so the
blade type should not matter.

Validated on hardware with firmware `DMN_XENO_B_SV1.4.0` (see section 5 of
[PROTOCOL.md](PROTOCOL.md)). Boards from other makers (Proffieboard, CFX,
Golden Harvest, Asteria...) use different protocols and are **not** supported.

## Entities

Entity IDs below are shown for an English-language install; HA derives them
from the translated entity name at creation time.

| Entity | Purpose |
|---|---|
| `light.<saber>_blade` | Blade on/off, RGB colour, brightness |
| `select.<saber>_sound_font` | Active sound font (1 to N) |
| `select.<saber>_blade_effect` | Blade effect (1 to N) |
| `number.<saber>_volume` | Volume 0–100 |
| `number.<saber>_blade_length` | Blade length in LEDs (disabled by default) |
| `sensor.<saber>_battery` | Battery % |
| `sensor.<saber>_font_name` | Active font name |
| `switch.<saber>_background_music` | Background music |
| `switch.<saber>_motion_control` | Gesture control |
| `switch.<saber>_lockup` / `_drag` | Sustained lockup / drag effect, on until turned off |
| `switch.<saber>_color_sync` | Blade follows a light and/or WLED (only if a source is set in the options) |
| `button.<saber>_clash` / `_blaster` / `_force` | Triggers the effect |
| `button.<saber>_reconnect` | Drops the Bluetooth link and opens a fresh session (diagnostic) |
| `binary_sensor.<saber>_connected` | Bluetooth link up (diagnostic) |
| `binary_sensor.<saber>_authorized` | Saber accepted the authorization (diagnostic) |

### Settings (configuration entities)

Everything the official app's settings screens offer, with the same ranges.
Some are disabled by default; enable them on the device page.

| Entity | Purpose |
|---|---|
| `number.<saber>_blade_brightness` | Blade brightness, 0–100 % (the same value as the light's brightness) |
| `number.<saber>_power_on_time` / `_power_off_time` | Button hold to wake from / enter deep sleep (1–10 s / 6–30 s) |
| `number.<saber>_auto_deep_sleep` | Idle time before deep sleep, 1–60 min |
| `number.<saber>_ignition_time` / `_retraction_time` | Ignition and retraction time of the current font, 0–60,000 ms |
| `number.<saber>_pre_on_time` | Pre-on light effect time, 0–5 s (disabled by default) |
| `number.<saber>_clash_sensitivity`, `_swing_…`, `_twist_…`, `_push_…`, `_pull_…` | Gesture sensitivities: **higher = more sensitive**, like the app's sliders |
| `number.<saber>_accent_swing_sensitivity` | Accent swing, 1–20 |
| `number.<saber>_side_blade_length` / `_side_blade_delay` | Cross-guard / double blade (disabled by default; unavailable without a side blade) |
| `number.<saber>_motor_speed` | Rotating crystal chamber, Pixel hardware only (disabled by default) |
| `select.<saber>_blaster_effect`, `_force_effect`, `_lockup_effect`, `_clash_effect`, `_post_off_effect` | Light effect for each event (unavailable when the font has none) |
| `select.<saber>_mode` | Blade mode |
| `switch.<saber>_flash_on_clash`, `_countdown`, `_led_screen` | Other switches from the app |
| `switch.<saber>_velocity_mode`, `_torch_mode`, `_ghost_mode`, `_lightning_block_mode`, `_blaster_mode`, `_multi_block_mode`, `_multi_lock_mode` | Blade modes |
| `switch.<saber>_pull_push_ignition`, `_push_pull_retraction`, `_twist_ignition`, `_twist_retraction`, `_swing_ignition` | Gestures (need *Motion control* on) |
| `switch.<saber>_fast_color_cycling` / `_smooth_color_cycling` | Colour cycling |
| `button.<saber>_save_settings` | Writes the settings to the saber's flash. The app has a separate save step, so press it after changing settings or they may not survive a reboot. |
| `button.<saber>_restart` | Reboots the saber |
| `sensor.<saber>_blade_firmware` / `_hardware_type` / `_buttons` | Board configuration (diagnostic) |
| `sensor.<saber>_file_transfer` | Progress of a running transfer, in % (diagnostic). For a whole font it covers all the files; the attributes give the file being sent (`path`) and `files_done` / `files_total` |

The board's hardware and firmware versions appear on the device page as soon as
the saber reports them.

## Actions

Sound fonts, files on the saber's card, firmware and board configuration are
handled by actions (*Developer tools → Actions*, or in scripts). Each takes
the saber as a device (`device_id`): the picker shows the names you gave the
devices.

Files on the Home Assistant host must be in a folder Home Assistant may read:
`/media` works out of the box, other folders go in
[`allowlist_external_dirs`](https://www.home-assistant.io/integrations/homeassistant/#allowlist_external_dirs).
Transfers, firmware and board changes refuse to run while the blade is lit,
like the app.

| Action | What it does |
|---|---|
| `xeno_saber_ble.upload_sound_font` | Sends a folder or a `.zip` of WAVs to font folder *N*, replacing that font. Xeno (`clash (1).wav`), Proffie (`clsh01.wav`, `clsh/clsh01.wav`) and simple (`clash1.wav`) names are recognised and renamed; files must be 44.1 kHz, 16-bit, mono. `dry_run` shows the renaming without sending anything. Checks free space first; `delete_first` frees the old font's room (export it first). The next free font number adds a new font, as in the app. Slow: count several minutes per megabyte. |
| `xeno_saber_ble.backup_saber` | Full backup into one `.zip`: every font (or the ones listed in `fonts`), `config.ini`, the custom button mapping if installed, the ~430 system sounds (voice prompts; the ones the firmware lacks are skipped) and `xeno_backup.json` (versions, current settings, font list). Laid out like a Xeno SD card (`1/`, `2/`…, `setting/`). A full saber takes hours over Bluetooth; the zip grows as `.part` meanwhile. |
| `xeno_saber_ble.restore_backup` | Writes a `backup_saber` zip (or a zip of a Xeno SD card) back: each font is replaced and renamed as in the backup, missing folders are created if they are the next free ones, then `config.ini` and the custom button mapping are written last. `fonts` restricts it to some folders, `include_settings: false` keeps the current settings (settings of a board with another hardware version are refused), `include_system_sounds` (off by default) rewrites the voice prompts. Needs `confirm: true`; `dry_run: true` only returns what would be written. |
| `xeno_saber_ble.export_sound_font` | Copies every file of font *N* from the card into a `.zip` on the HA host (backup, editing). `upload_sound_font` takes that zip back. As slow as an upload: the zip grows as `<name>.zip.part` and gets its final name when the font is complete. |
| `xeno_saber_ble.copy_sound_font` | Copies font *A* over font *B* on the card (the saber copies the files itself, so it is quick), and gives the copy the source's name unless you set one |
| `xeno_saber_ble.list_sound_fonts` | Font folders, their names (when the saber reports them) and storage used/total/free in MB |
| `xeno_saber_ble.list_sound_files` | The files of one font, by type |
| `xeno_saber_ble.rename_sound_font` | Renames a font |
| `xeno_saber_ble.play_sound` | Plays a file from the card, or stops |
| `xeno_saber_ble.upload_file` / `download_file` | Copies a file to / from the card, for example `config.ini` to back up or restore the settings |
| `xeno_saber_ble.copy_file` / `delete_file` | Copies or deletes a file on the card |
| `xeno_saber_ble.update_firmware` | Sends a vendor `update.bin`; the saber installs it and reboots. Needs `confirm: true` and 30 % battery. |
| `xeno_saber_ble.set_blade_firmware` | Switches between Pixel and RGB blade firmware, then reboots. Needs `confirm: true`. |
| `xeno_saber_ble.set_hardware_type` | Sets how the board is wired and its button count, then reboots. The app warns this requires soldering. Needs `confirm: true`. |

> [!CAUTION]
> `update_firmware`, `set_blade_firmware` and `set_hardware_type` can leave the
> saber unusable if the file, the type or the timing is wrong. A firmware newer
> than the tested one may also stop this integration from working. Keep the
> saber close to the adapter, charged, and out of use until it has restarted.

Example, replacing font 3 and naming it:

```yaml
action: xeno_saber_ble.upload_sound_font
data:
  device_id: 3f2a...   # pick the saber in the UI
  folder: /media/fonts/Kyber Workshop.zip   # or a folder
  font: 3
  name: Kyber Workshop
```

Transfers need a Bluetooth link with a large MTU (at least 508), which local
adapters and recent ESPHome proxies negotiate; the action says so if the link
is too small.

Sabers sold without an SD card keep their fonts in internal memory, and these
actions work on it (one board showed its 18 built-in fonts, 479 MB in all with
22 MB free). Those sounds exist nowhere else: run `backup_saber` once before replacing
any font, and `restore_backup` puts them back. Each font is emptied before its
files are sent, so if the saber runs out of room partway, that font stays empty
until a restore with more free space. The backup zip has the layout of a Xeno SD card, so its contents can also
be copied onto a card (keep `xeno_backup.json` off the card). With an SD card inserted, the saber
uses the card instead.

## Installation

1. Copy `custom_components/xeno_saber_ble/` into the `custom_components/`
   folder of your HA config (or add this repository as a HACS custom repository).
2. Restart Home Assistant.
3. **Close the Xeno Configurator app** on your phone: the saber only accepts
   one BLE connection at a time.
4. Wake the saber (hold the button ~2 s). HA discovers it automatically;
   otherwise go to *Settings → Devices & services → Add integration →
   Xeno Saber BLE*.

Setup asks for nothing but the saber to add: confirm the discovered saber, or
pick it from the list of sabers currently awake. Home Assistant then connects
once to check that the saber answers; if it does not, you get *Could not
connect* and can retry after waking it or closing the phone app. Everything
else is set later in the options.

Boards all advertise the name *Saber*, so a new saber is named after the end
of its Bluetooth address, e.g. *Saber EE:FF*. Rename it either way, the device
or the entry: the other follows, so each saber has one name everywhere.

A sleeping saber is not an error: Home Assistant starts with its entities
unavailable and connects when the saber wakes up and advertises.

Home Assistant 2026.3 or later shows the integration's own icon and logo
(shipped in `brand/`, with dark-theme variants); older versions show a
generic icon. To change them, edit `tools/brand_svg.py` and run
`python tools/make_brand.py`.

If the icon does not appear: Home Assistant only looks for `brand/` when it
starts, so **restart Home Assistant** (a reload of the integration is not
enough), then force-refresh the browser (Ctrl+Shift+R) or reset the companion
app's frontend cache.

## Options

*Settings → Devices & services → Xeno Saber BLE → Configure*

### Persistent or on-demand connection (*Disconnect after idle*)

- **0 (default)**: HA stays connected. State is live (hilt button, battery),
  and HA reconnects as soon as the saber wakes up. The trade-off is that the
  phone app cannot connect while HA holds the saber.
- **N seconds**: HA connects only to send a command, then releases the saber
  after N seconds. The phone app stays usable; the displayed state is only
  current while connected.

### Keepalive: drains the battery

> **Warning: keepalive drains the saber's battery.** While it is on, the saber
> never goes to sleep: its board and Bluetooth radio stay powered all the
> time, even with the blade off. Use it only while the saber sits on its
> charger, or for short sessions.

*Keepalive interval* is **0 (off) by default**. Any other value resends the
saber's current brightness every N seconds, which changes nothing visible but
resets its idle-sleep timer. It needs a permanent connection (*Disconnect after
idle* = 0), and it never wakes a saber that is already asleep or out of range.

While keepalive is on, Home Assistant shows a warning under
*Settings → Repairs* and in the log, naming the saber and the interval. Set the
interval back to 0 to remove it.

### Color sync

The blade can follow another light, the way WLED devices sync with each other.
Set one or both sources in the options, then use the **Color sync** switch to
start and stop following. The switch only exists once a source is set, and it
keeps its state across restarts.

- **Light to follow**: any light entity in HA (a WLED, a Hue bulb, another
  saber's blade). The blade copies its on/off state, color and brightness, and
  matches it as soon as sync is switched on. **Turning that light on ignites
  the blade.** A light that becomes unavailable leaves the blade as it is.
- **WLED broadcasts**: listens on UDP port 21324 for WLED's sync packets (in
  WLED: *Settings → Sync Interfaces → Send notifications on change*). WLED
  brightness 0 turns the blade off. The port is only open while at least one
  saber has sync on, and one socket is shared by all sabers.
  **These packets are not authenticated**: any device on your network can
  drive the blade. Set *WLED source address* to the IPv4 address of your WLED
  device to ignore everyone else.

Solid colors sync reliably. Animated WLED effects are approximate: WLED only
broadcasts its primary color when it changes, and each Bluetooth write takes
about 100 ms, so fast effects are thinned out (only the latest value is sent).

## How data is updated

There is no polling. The saber pushes its state over Bluetooth notifications
(blade on/off, colour, font, volume, battery...) after every change, including
changes made with the hilt button. With a permanent connection (the default)
entities are updated live. In on-demand mode they are updated only while Home
Assistant is connected, that is for a few seconds around each command; the
last known values are shown in between. The battery level is the saber's own
estimate, refreshed whenever it sends its state.

## Range: Bluetooth proxies

If the saber is out of range of the HA host, add any ESPHome device running the
stock [`bluetooth_proxy`](https://esphome.io/components/bluetooth_proxy.html)
component near it. HA routes the connection through the proxy automatically;
no saber-specific firmware is needed. An ESP32 handles up to three active
connections, so one proxy can serve several sabers.

## Testing without Home Assistant

`tools/xeno_cli.py` reuses the integration's own code:

```bash
pip install bleak bleak-retry-connector
python tools/xeno_cli.py scan                 # add --all to list every named device
python tools/xeno_cli.py --adapter hci1 scan  # pick a specific adapter (Linux)
python tools/xeno_cli.py state  AA:BB:CC:DD:EE:FF      # dump state
python tools/xeno_cli.py on     AA:BB:CC:DD:EE:FF
python tools/xeno_cli.py color  AA:BB:CC:DD:EE:FF 0 255 0
python tools/xeno_cli.py set    AA:BB:CC:DD:EE:FF Volume 60
python tools/xeno_cli.py raw    AA:BB:CC:DD:EE:FF '[2,{"Force":true}]'
python tools/xeno_cli.py -v listen AA:BB:CC:DD:EE:FF   # follow notifications
```

Start with `state`: if you get JSON containing `PowerOn`, `Power`,
`BackgroundColor`, etc., the protocol matches your board.

If BlueZ reports `org.bluez.Error.InProgress`, another program is scanning on
the same adapter (GNOME/KDE Bluetooth settings, Blueman, `bluetoothctl scan on`).
Close it, or run `bluetoothctl scan off`, then retry.

## Use cases

- **Alarm or doorbell signal**: turn the blade red when the alarm is armed, or
  flash it when someone rings.
- **Match the room**: with *Color sync*, the saber on its stand follows your
  ambient lights or a WLED strip.
- **Movie night**: a scene that dims the lights and ignites the blade in the
  film's colour, with a font of your choice.
- **Battery reminder**: get a notification when the saber needs charging.
- **Display piece**: on external power, keep it reachable with the keepalive
  option (see the battery warning above) and light it from automations.

## Examples

```yaml
automation:
  - alias: "Red saber when the alarm is armed"
    triggers:
      - trigger: state
        entity_id: alarm_control_panel.home
        to: armed_away
    actions:
      - action: light.turn_on
        target:
          entity_id: light.xeno3_blade
        data:
          rgb_color: [255, 0, 0]

  - alias: "Saber needs charging"
    triggers:
      - trigger: numeric_state
        entity_id: sensor.xeno3_battery
        below: 20
    actions:
      - action: notify.notify
        data:
          message: "The saber's battery is at {{ states('sensor.xeno3_battery') }} %."

  - alias: "Movie night"
    triggers:
      - trigger: state
        entity_id: input_boolean.movie_night
        to: "on"
    actions:
      - action: select.select_option
        target:
          entity_id: select.xeno3_sound_font
        data:
          option: "3"
      - action: light.turn_on
        target:
          entity_id: light.xeno3_blade
        data:
          rgb_color: [0, 120, 255]
          brightness_pct: 60
```

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| The saber is not discovered | It is asleep or off: hold the button to wake it. Check that it is in range of the HA host or of a Bluetooth proxy. |
| *Could not connect* during setup, or commands fail | The saber accepts **one connection at a time**: close the Xeno Configurator app. If the phone must keep working, use on-demand mode in the options. |
| Entities become unavailable | The saber was switched off with the hilt button or fell asleep; its radio is then off and HA reconnects once it advertises again. To keep it awake, see *Keepalive*. |
| Several sabers: one keeps failing, or commands are slow | The sabers share the Bluetooth adapter. Connection attempts take turns, and a saber that cannot be reached is retried after 10 s, then less and less often (up to every 5 min). For two or more sabers, prefer *Disconnect after idle* > 0 (on-demand) for those you rarely use, or give each its own ESPHome Bluetooth proxy. |
| Frequent disconnections, or failures at a few metres | The board's radio is weak inside a metal hilt (a median of about −80 dBm in a capture made with the phone app). Add an ESPHome Bluetooth proxy near the saber's usual spot, keep the emitter end facing it, and keep the saber out of metal cases. *Download diagnostics* shows the last signal strength (RSSI) and which adapter or proxy heard it. |
| *Color sync* will not turn on | No source is set in the options, or UDP port 21324 is already used by another program on the HA host. |
| The icon or logo does not appear | See the note under *Installation*: restart HA, then refresh the browser. |
| `org.bluez.Error.InProgress` with the CLI tool | Another program is scanning on the same adapter (see *Testing without Home Assistant*). |
| *File transfer needs a Bluetooth MTU of at least 508* | The adapter or proxy negotiated a small MTU. Use the HA host's adapter, or an ESPHome proxy close to the saber. |
| Other commands fail with *A file transfer is in progress* | A sound font or firmware transfer is running; wait for `sensor.<saber>_file_transfer` to go back to 0. |
| Settings revert after the saber restarts | Press *Save settings* after changing them (the app also saves in a separate step). |

To report a problem, attach the diagnostics file (*Settings → Devices &
services → Xeno Saber BLE → ⋮ → Download diagnostics*; the Bluetooth address is
redacted) and debug logs:

```yaml
logger:
  logs:
    custom_components.xeno_saber_ble: debug
```

Connection losses are logged once at the *info* level, and so is the
reconnection, so a normal log is enough to see when the saber dropped out.

## Known limitations

- **Power off and sleep**: turning the saber off from the hilt, or letting it
  fall asleep after its idle delay, shuts its Bluetooth off completely. HA
  cannot wake it; someone has to press the button. Entities become unavailable
  meanwhile, and HA reconnects on its own once the saber advertises again.
- **Charging**: some boards refuse to light the blade while charging.
- **Range**: a few metres through walls with the stock board (see
  *Troubleshooting*); Bluetooth proxies are the fix.
- **One connection at a time**: Home Assistant and the phone app cannot be
  connected together.
- **Firmware updates**: validated on one board (firmware `DMN_XENO_B_SV1.4.0`,
  see section 5 of PROTOCOL.md). Other versions, including future updates, may
  change the protocol and break the integration (see the warning at the top).
  If that happens, please open an issue with the diagnostics file: it shows the
  reported firmware version.
- **Security**: the protocol has no real authentication (a static password
  shared by every saber). Anyone in BLE range can control the saber; this
  integration does not create that weakness, it documents it.
- **Firmware update and board configuration** (`update_firmware`,
  `set_blade_firmware`, `set_hardware_type`): implemented from the app's code
  and tested against a simulated saber, not yet on a real one. File transfers,
  sound fonts and backups are confirmed on a real saber.
- **Custom button and gesture mappings** (the app's function editor) have no
  editor in Home Assistant yet. Their file format is documented in section 8
  of [PROTOCOL.md](PROTOCOL.md). `backup_saber` and `restore_backup` save
  and put back the installed mapping.
- **Color sync and keepalive keep the radio busy**: frequent writes keep the
  saber awake (see the keepalive warning above) and share airtime with any
  other device on the same Bluetooth proxy.

## Removal

1. *Settings → Devices & services → Xeno Saber BLE*, open the saber's entry,
   ⋮ → **Delete**. This disconnects the saber, removes its device and entities,
   and clears the keepalive warning if there was one.
2. If you installed with HACS, remove the repository from HACS; otherwise
   delete `custom_components/xeno_saber_ble/` from your configuration folder.
3. Restart Home Assistant.

Nothing is changed on the saber itself; its settings stay as they were.

## Translations

User-facing text lives in `custom_components/xeno_saber_ble/strings.json`
(English source) and `translations/<lang>.json`. To add a language, copy
`translations/en.json` to `translations/<lang>.json` using a
[Home Assistant language code](https://developers.home-assistant.io/docs/internationalization/core/),
translate the values (keep keys and `{placeholders}` unchanged), and run the
tests: `tests/test_translations.py` checks that every file matches the source.

## Tests

```bash
pip install -r requirements_test.txt
# Protocol, BLE client and file transfer against a simulated saber, sound font
# naming, WLED listener, translation files
pytest tests/test_protocol.py tests/test_device.py tests/test_device_files.py \
  tests/test_transfer.py tests/test_soundfont.py tests/test_wled_udp.py \
  tests/test_translations.py -p no:homeassistant
# Full integration inside a Home Assistant instance
pytest tests/ha -o asyncio_mode=auto
ruff check .
mypy custom_components/xeno_saber_ble   # strict, same settings as Home Assistant core
```

GitHub Actions runs the same tests (coverage must stay above 95 %), Ruff,
strict mypy, `hassfest` and the HACS validation on every push and pull request.
`custom_components/xeno_saber_ble/quality_scale.yaml` records how each rule of
Home Assistant's
[Integration Quality Scale](https://developers.home-assistant.io/docs/core/integration-quality-scale/)
is met (target: Platinum).

## Credits

Keepalive, lockup/drag, the diagnostic entities and WLED sync were ported from
[Ollec's ha-xenopixel](https://github.com/Ollec/ha-xenopixel) ESPHome proxy
(MIT). Its protocol captures also confirmed the `Lockup` and `Drag` keys. The
code here is a separate Python implementation.

## Authorized use

Use this integration **only with your own saber** or with its owner's consent.
Taking control of someone else's device without their consent may be illegal.

## Legal notice

Independent, unofficial project, not affiliated with or endorsed by LGT,
Damien Technology (Damiensaber), Lucasfilm or their resellers. "Xenopixel",
"Xeno Configurator" and other marks mentioned belong to their respective
owners and are used only to indicate compatibility.

This repository does not contain the manufacturer's app, decompiled code, or
assets (images, sounds) taken from it, and must not receive any. The protocol
was documented to enable interoperability, in accordance with section 30.61 of
the Copyright Act (Canada).

Released under the [MIT License](LICENSE), without warranty.
