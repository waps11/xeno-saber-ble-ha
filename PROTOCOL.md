# BLE protocol of Xenopixel V3 compatible boards

This document describes only what is needed to make third-party software (here,
Home Assistant) interoperable with sabers that use a Xenopixel V3 board. It was
established from the official *Xeno Configurator* 1.5.2 app
(`com.xeno.configurator`, versionCode 25), lawfully obtained, under the
interoperability exception of the Canadian Copyright Act (s. 30.61). No code
from the app is reproduced here or anywhere in this repository.

The app's code lives in the `com.damien.damiensaber` package; its BLE layer uses
the FastBle library (`com.clj.fastble`).

Reference classes (jadx names):

| Role | Class |
|---|---|
| UUIDs, handshake, authorization | `com.damien.damiensaber.utils.f` |
| Command encoding | `com.damien.damiensaber.vo.Type#sendData` |
| State model | `com.damien.damiensaber.vo.BleData` |
| Connection sequence, notification parsing | `w5.d` (DeviceViewModel) |
| Main screen (ignition, battery) | `r6.m` |
| Settings screens (ranges) | `r6.e`, `r6.o`, `r6.s`, `r6.u`, `q6.e0` |
| Board configuration, firmware update | `ui.activity.DeviceSettingActivity` |
| File-system commands and replies | `command.LSFileCommondType`, `b6.a` |
| File transfer | `t6.e` (send), `t6.g` (receive), `t6.h`, `t6.a` (CRC) |
| Custom mappings (`customfunction.diy`) | `c6.a` (read), `u6.s` (write), `command.function.EVENT`, `room.function.*`, `utils.j` (AES) |

## 1. Transport

No encryption, no BLE pairing, no signing. Messages are **plain UTF-8 JSON**
written to GATT characteristics.

| Service | Characteristic | Use |
|---|---|---|
| `0000dae0-0000-1000-8000-00805f9b34fb` | `0000dae1-…` | Handshake, state pushes (read, write, notify) |
| `00003ab0-0000-1000-8000-00805f9b34fb` | `00003ab1-…` | Commands, authorization (read, **write-without-response**, notify) |

The app's scanner filters on these two service UUIDs. A real board advertises:

- local name `Saber`, public MAC address, no pairing or bonding;
- both service UUIDs `0xDAE0` and `0x3AB0`;
- manufacturer data with ID `0x6144` (24900) and payload `mien`: read as
  little-endian bytes, the ID is `Da`, so the whole field spells `Damien`.

Each message goes out as a single write (`split = false`). `DAE1` takes
writes with response; `3AB1` only accepts writes **without** response, so a
client must pick the write type from the characteristic's properties. The app negotiates an **MTU of 512**
first. Command messages are under 60 bytes; state notifications can be longer,
so a client must either negotiate a large MTU or reassemble fragments.

The UUIDs `0000baad-…`, `0000f00d-…` and `dfd84064-3d8a-11ed-b878-0242ac120002`
are declared in `r6.m` but never used.

## 2. Message format

```
[<type>, {<key>: <value>, ...}]
```

| `type` | Meaning | Direction |
|---|---|---|
| `2` | Write / command | app → saber |
| `3` | Reply / state | saber → app |
| `4` | Error (`{"<key>": "<message>"}`) | saber → app |

A command is a two-element JSON array: the integer `2`, then an object holding
a single pair whose key is the setting name (case-sensitive) and whose value is
a JSON literal. For example `[2,{"Volume":60}]`. Value encoding rules:

- booleans: `true` / `false`
- integers: decimal
- strings: quoted (except for "unformatted" writes)
- `BackgroundColor`: raw JSON array `[r,g,b]`, **not** quoted

There is **no read command**. The saber pushes its state as `[3,{...}]`
notifications after the handshake and after every change, including changes
made on the hilt (font, colour, effects). On real hardware these pushes arrive
on `DAE1`, so a client must keep notifications enabled on both characteristics. A notification may
carry one or more keys; the app merges them into `BleData` by reflection.

## 3. Connection sequence

1. GATT connect, request MTU 512.
2. Enable notifications on `DAE1`.
3. Write to `DAE1`: `[2,{"HandShake":"HelloDamien"}]`
   → the full state dump, split over about three `[3,{...}]` notifications on
   `DAE1`; the last one ends with `"HandShake":"OK"`.
4. Enable notifications on `3AB1`.
5. Write to `3AB1`: `[2,{"Authorize":"SaberOfDamien"}]`
6. Read `3AB1` → expected `[3,{"Authorize":"AccessAllowed"}]`.

The "authorization" is a static hard-coded password, identical for every saber.
It is not a security mechanism: any BLE client in range can control any
compatible saber. The app itself does not even wait for the read result before
sending its next command.

## 4. State and command keys

Every key of the `Type` enum is accepted as a write in the form
`[2,{"Key":value}]`. The most useful ones:

| Key | Type | Meaning | Example |
|---|---|---|---|
| `PowerOn` | bool | Blade lit / off | `[2,{"PowerOn":true}]` |
| `Power` | int | **Battery %** (read-only) | `[3,{"Power":87}]` |
| `BackgroundColor` | `[r,g,b]` | Blade colour | `[2,{"BackgroundColor":[255,0,0]}]` |
| `Brightness` | int 0–100 | Brightness | `[2,{"Brightness":80}]` |
| `Volume` | int 0–100 | Volume | `[2,{"Volume":50}]` |
| `CurrentSoundPackageNo` | int, 0-based | Active sound font | `[2,{"CurrentSoundPackageNo":3}]` |
| `TotalSoundPackage` | int | Number of fonts (read) | |
| `fontname` | string | Active font name (read) | |
| `CurrentLightEffect` | int, 0-based | Blade effect | |
| `TotalLightEffect` | int | Number of effects (read) | |
| `CurrentMode` / `TotalMode` | int | Mode | |
| `MainBladeLength` | int 0–144 | Blade length (LEDs) | |
| `BGMOn` | bool | Background music | |
| `MotionControl` | bool | Gesture control | |
| `Clash` / `Blaster` / `Force` | `true` | Triggers the effect | `[2,{"Clash":true}]` |
| `Lockup` / `Drag` | bool | Sustained effect (on/off); echoed back as `[3,{"Lockup":true}]` | `[2,{"Lockup":true}]` |
| `FastColorChanging` / `SmoothColorChanging` | bool | Colour cycling | |
| `SaveCfg` | `true` | Saves the current settings to flash (the app's separate *save* step) | |
| `Reset` | `true` | **Reboots** the saber (not a factory reset, see §5) | |
| `HardwareVersion` / `SoftwareVersion` | string | Versions (read) | |

Other keys (gesture settings, timings, modes): `PreonTime`, `SideBlade`,
`SideBladeLength`, `SideBladeDelay`, `ClashSensitivity`, `AwakenTime`,
`SleepTime`, `IdleSleepTime`, `PullPushSwitchOn`, `PushPullSwitchOn`,
`PullSensitivity`, `PushSensitivity`, `TwistOnSwitchOn`, `TwistOffSwitchOn`,
`TwistSensitivity`, `SwingSwitchOn`, `SwingSensitivity`,
`SwingSoundSensitivity`, `Countdown`, `ClashOn`, `MotorSpeed`, `VelocityMode`,
`TorchMode`, `GhostMode`, `LightningMode`, `BlasterMode`, `BlockMode`,
`BattleMode`, `ButtonNum`, `Matrix`, `OnTime`, `OffTime`.

`Lockup` and `Drag` are absent from the state dump until they are first
toggled; a client should treat them as off until then. The dump's
`CurrentLockup` / `CurrentDrag` are the selected effect *variants*, not whether
the effect is active. (Behaviour also observed in the captures of
[Ollec/ha-xenopixel](https://github.com/Ollec/ha-xenopixel).)

The app's sensitivity sliders send `max - progress`: a lower value means more
sensitive.

### Ranges allowed by the app

| Key | App label | Range | Unit / note |
|---|---|---|---|
| `Brightness` | brightness | 0–100 | %, sent as is |
| `AwakenTime` | power on time | 1000–10000 | ms (0.1 s steps): button hold to wake from deep sleep |
| `SleepTime` | power off time | 6000–30000 | ms (0.1 s steps): button hold to enter deep sleep |
| `IdleSleepTime` | auto deep sleep | 1–60 | minutes |
| `OnTime` / `OffTime` | out time / in time | 0–60000 | ms, typed in; ignition and retraction of the current font |
| `PreonTime` | light effect time | 0–5000 | ms; greyed out in the app when 0 |
| `SideBladeDelay` | side startup delay | 0–5000 | ms; only with `SideBlade` |
| `MainBladeLength` | main blade length | 0–144 | LEDs; only enabled when `MainBlade` is true |
| `SideBladeLength` | side blade length | 0–144 | LEDs; only with `SideBlade` |
| `ClashSensitivity` | flash on clash sensitivity | 0–30 | slider sends `30 - position` |
| `PushSensitivity` / `PullSensitivity` | push / pull | 0–50 | slider sends `50 - position` |
| `SwingSensitivity` / `TwistSensitivity` | swing / twist | 0–1000 | slider sends `1000 - position` |
| `SwingSoundSensitivity` | accent swing sensitivity | 1–20 | sent as is |
| `MotorSpeed` | motor speed | 0–100 | %; only when `HardwareType` is `Pixel` |
| `CurrentBlaster`, `CurrentForce`, `CurrentLockup`, `CurrentClash`, `CurrentPostOff`, `CurrentMode` | effect choices | 0 to `Total…` − 1 | `Total… = 0` means none to choose |

Booleans on the settings screen: `Countdown`, `Matrix` (LED screen),
`MotionControl`, `ClashOn` (flash on clash), `VelocityMode`, `TorchMode`,
`GhostMode`, `LightningMode` (lightning block), `BlasterMode`, `BlockMode`
(multi-block), `BattleMode` (multi-lock), and the gestures `PullPushSwitchOn`,
`PushPullSwitchOn`, `TwistOnSwitchOn`, `TwistOffSwitchOn`, `SwingSwitchOn`.
`FastColorChanging` and `SmoothColorChanging` start colour cycling from the
colour screen. `CurrentDrag` is reported but the app never writes it.

## 5. Board configuration

- `SoftwareType` (`"Pixel"` or `"RGB"`) selects the blade firmware. The app
  warns that the blade must match, then sends `Reset` so the saber reboots.
- `HardwareType` (`"Pixel"` or `"RGB"`) and `ButtonNum` (1 or 2) describe how
  the board is wired. The app warns that changing the hardware type requires
  soldering, sends both, then `Reset`.
- `Reset` is only ever sent after one of these changes: it reboots the saber.
  The app's "reset to factory settings" is a different operation that
  downloads factory firmware and settings from the vendor and sends them with
  the file transfer below.

The app refuses file transfers, firmware updates and factory resets while the
blade is lit ("Please shut down before operation").

## 6. File system

The saber exposes its SD card. Font folders are numbered from 1; each holds
files named `<type> (<n>).wav`, numbered from 1 without gaps, for example
`/3/clash (2).wav`. The settings file is addressed as `config.ini` (the saber
reports it as `/setting/config.ini`). Sound files are 44.1 kHz, 16-bit, mono
PCM WAV; the app converts other audio to that before sending.

Commands use the ordinary command format; the reply comes back as a state
message with the same key:

| Command | Value | Reply |
|---|---|---|
| `ls` | `"/"` | first and last font folder numbers, then one `fontname` message per font: `["/<n>", "<name>"]` |
| `ls` | `"/<n>"` | 56 integers: the first and last file number of each sound type, in the order below; `0, 0` = none |
| `df` | `""` | `[used, total]` in MiB (the app shows `M`, or `G` above 1024) |
| `setname` | `["/<n>", "<name>"]` | the same pair: renames font `n`; on the next free number it **creates** the folder (the app's "new font" names it after its number). Names are up to 16 characters in the app |
| `play` | `"/<n>/<file>.wav"`, or `"null"` to stop | an integer |
| `cp` | `["<from>", "<to>"]` | `true` on success |
| `rm` | `"<path>"` | `true` / `false` |

On real hardware the saber answered `ls "/"` without any `fontname`
messages, so names are only known from `setname` replies and, for the active
font, from the `fontname` state key (`CurrentSoundPackageNo` is the 0-based
position in the folder list).

A saber sold without an SD card exposes its internal memory here: the tested
board listed its 18 built-in fonts with `df` = `[457, 479]` (479 MiB, 22 MiB
free) and no card inserted.

Sound type order for `ls "/<n>"`: begindrag, beginlock, beginmelt,
beginlightningblock, blaster, clash, drag, enddrag, endlock, endmelt,
endlightningblock, font, force, hum, in, lock, lightningblock, melt, out, spin,
stab, postoff, preon, sideout, swing, swingh, swingl, track.

The settings folder holds `config.ini`, `customfunction.diy` (only when a
custom mapping is installed, see §8) and the system sounds: voice prompts and
menu sounds such as `appconnected.wav`, `batterylevel.wav`, and the numbered
`blade (n)`, `lighteffect (n)`, `power (n)`, `volume (n)` (1–100) and
`saber (n)` (1–7). These are the names on the vendor's default card. `ls`
cannot list this folder, so a reader has to try known names.

Writing back follows the same names: a font file goes to `/<n>/<type> (<k>).wav`,
a system sound to `/setting/<name>.wav`, the settings to `config.ini` and
`/setting/customfunction.diy`. A font folder that does not exist yet is created
first with `setname` on the next free number (§6); `ls` only reports a range per
sound type, so replacing a font means removing its old files with `rm`.

## 7. File transfer

Files move over `3AB1` with a variant of YMODEM. While a transfer runs, every
notification on `3AB1` belongs to it; ordinary state pushes can still arrive
and should be applied as usual.

1. The client sends `[2,{"FileTransfer":["<direction>","<path>"]}]`.
   The direction is seen from the saber: `download` sends a file **to** the
   saber, `upload` reads one **from** it. The saber is expected to answer
   with a state message echoing `FileTransfer`, but the app takes **any**
   notification on `3AB1` as the answer. It sends the command again if
   nothing comes (after 1 s when reading, 10 s when sending).
2. **Sending to the saber.** The client sends a header block: byte `0x01`,
   block number `0x00`, its complement `0xFF`, 128 bytes holding the file name
   (the destination path), a NUL, the size in decimal, a NUL and an MD5 that
   the app leaves empty, zero-padded; then a CRC. The saber answers `ACK`
   (`0x06`), then `C` (`0x43`) in a separate notification. Data then goes in
   blocks of **500 bytes**: byte `0x11`, block number (starting at 1, wrapping
   at 256), its complement, the data padded with `0x1A`, and a CRC; 505 bytes
   in all, so the link needs an ATT MTU of at least 508. Each block waits for
   `ACK`; `NAK` (`0x15`) asks for it again and `CAN` (`0x18`) aborts. The
   client ends with `EOT` (`0x04`); the saber answers `NAK`; the client sends
   `EOT` again and the saber answers `ACK`. There is no closing empty header.
3. **Reading from the saber.** After the saber's echo, the client sends `C`.
   The saber sends the header block (name and size as above), the client
   answers `ACK`, waits about 100 ms and sends `C`. Each 505-byte data block is
   answered with `ACK`. On the first `EOT` the client answers `NAK`, on the
   second `ACK`.

CRCs are CRC-16/XMODEM (polynomial `0x1021`, initial value 0) over the 128 or
500 data bytes, big-endian. The app waits 10 s for each answer (1 s when
reading) and retries a packet once.

A firmware update is a transfer of the vendor's image to `update.bin` with
header name `update.bin`; after the final `ACK` the saber installs it and
reboots. The app looks up images in a JSON index on the vendor's server, keyed
by the start of `SoftwareVersion` (`_XENO_` replaced by `_XRGB_` when
`HardwareType` is `RGB`). The integration does not download firmware itself.

## 8. Custom button and gesture mappings (`customfunction.diy`)

The app's function editor decides which button presses and gestures trigger
which action. It stores the result in `/setting/customfunction.diy` on the card:

- to read it, the app runs a transfer from the saber (`upload`) of
  `/setting/customfunction.diy`;
- to install a mapping, it sends the file with header name
  `/setting/customfunction.diy`;
- to go back to the built-in behaviour, it deletes the file
  (`rm "/setting/customfunction.diy"`).

The state key `FileID` is `[1, n]`: `n` is 0 when no custom file is installed;
otherwise it is the file's checksum field (bytes 12–15 below) read as a
little-endian 32-bit integer. The app compares it after an upload to confirm
the saber took the file.

### File layout

As the app writes it: a 16-byte header, then the body. The body is 2,100
bytes of plain data, zero-padded to 2,112 and encrypted. That makes 2,128 bytes
in all.

| Bytes | Content |
|---|---|
| 0–3 | `XENO` (ASCII) |
| 4–6 | `01 00 00` (file type) |
| 7 | `01` = body encrypted, `00` = body in clear (the app always writes `01`, and reads both) |
| 8–9 | "Delay time during controls", in ms, little-endian 16-bit; the app writes 600 when the value is under 500 |
| 10 | "Multi control tone": `01` on, `00` off |
| 11 | `00` |
| 12–15 | CRC-32 (IEEE, as computed by zlib) of the 2,100-byte plain body, **big-endian** |
| 16– | Body |

**Encryption.** AES-128 in CBC mode without padding. The key is the 16 ASCII
bytes `WatFGRMjAxODA5MT` and the IV the 16 ASCII bytes `updatesuccessful`,
both fixed in the app. Decrypt, then keep the first 2,100 bytes. The app
refuses files shorter than 2,116 bytes.

**Body.** 25 functions of 84 bytes each, in the order of the table below.
Each function has 4 *trigger slots*, stored as:

- 4 slots × 5 action words, each word a 32-bit **little-endian** integer
  (80 bytes; a 0 word is empty);
- then 4 bytes, one per slot: the blade orientation the slot requires
  (`0` any, `1` pointing up, `2` horizontal, `3` pointing down).

The app shows a function's slots, and the words inside a slot, as separate
triggers for the same action. Its own default mapping uses one word per
slot; how the firmware treats several words in one slot is not confirmed.

| # | Function (app label) | Blade | # | Function (app label) | Blade |
|---|---|---|---|---|---|
| 0 | ignite (out) | off | 13 | infinite colour change | on |
| 1 | retract (in) | on | 14 | drag | on |
| 2 | announce battery level | off | 15 | force | on |
| 3 | background music (track) | off | 16 | previous font | off |
| 4 | blaster | on | 17 | lockup | on |
| 5 | enter multi-lock mode | on | 18 | lightning block | on |
| 6 | exit multi-lock mode | on | 19 | melt | on |
| 7 | enter multi-block mode | on | 20 | next font | off |
| 8 | exit multi-block mode | on | 21 | next light effect | off |
| 9 | clash | on | 22 | next blade mode | off |
| 10 | colour selected (end colour change) | on | 23 | stab | on |
| 11 | twist colour change | on | 24 | adjust volume | off |
| 12 | standard (step) colour change | on | | | |

"Blade" is when the function applies; it sets bit 10 of the action words.

**Action word.**

| Bits | Meaning |
|---|---|
| 24–31 | Event code (below) |
| 17 | Set for the second button (dual-button hilts); clear for the main button |
| 10 (`0x400`) | Set when the function applies with the blade on (column above); clear for blade-off functions |
| 0–1 | Gestures only: button held during the gesture, `1` main, `2` second, `0` none |

The packing (event in the top byte, button and modifier bits below) and the
event numbering follow the `EVENT` list of ProffieOS, which the app copies.

**Event codes.** For press-and-release durations `t` = 500, 1000, … 9500 ms,
let `p = t / 500 − 1` (0–18). For hold durations 1000, 1500, 2000, 3000 and
5000 ms, let `h` = 0–4 respectively.

| Trigger | Code |
|---|---|
| *n* clicks (*n* = 1–5) | 38, 67, 96, 125, 154 |
| `click_0` (ProffieOS `EVENT_CLICK_SHORT`; not used by the app's default) | 8 |
| hold the button for a duration *h* | 32 + *h* |
| press and release after *t* | 40 + *p* |
| *k* clicks, then hold (*k* = 1–4) | 61 + 29(*k*−1) + *h* |
| *k* clicks, then press and release after *t* (*k* = 1–4) | 69 + 29(*k*−1) + *p* |
| swing | 178 |
| twist, free direction | 180 |
| twist clockwise then anticlockwise | 181 |
| twist anticlockwise then clockwise | 182 |
| clash | 185 |
| push | 187 |
| pull | 189 |

Example: "pull ignites the blade" is function 0 (blade off, so bit 10 clear),
the main button is not involved, event 189: word `0xBD000000`, stored as
`00 00 00 BD`.

**Quirks of the app's reader.** It expects the second button as the value `1`
in bits 16–23, although its writer sets bit 17 (value `2`). It also looks for
the click-then-hold/press detail in bits 0–7, although its writer only puts
that in the event code. A third-party tool should follow the writer.

**Verification.** The layout, checksum and encryption come from the app's
code. The checksum convention is confirmed against the app's own constant for
an empty mapping: the CRC-32 of 2,100 zero bytes is `0x585684C3`, which the
app keeps byte-reversed as `C3845658`. The app also ships a default mapping
whose stored checksum this encoding does not reproduce, so the copy in the
app may be stale. Nothing here has been tried on a saber yet. Back up the
existing file with `download_file` before installing one.

## 9. Hardware validation

Tested on a Xenopixel V3 board reporting `HardwareVersion`
`XENOA0422xxxxxxxx`, `SoftwareVersion` `DMN_XENO_B_SV1.4.0`,
`HardwareType` `RGB` and `SoftwareType` `Pixel`.

Confirmed:

- handshake, authorization (`AccessAllowed`) and the full state dump;
- `PowerOn`, `BackgroundColor` and `Volume` writes take effect and are echoed
  back in the state;
- `Volume` and `Brightness` range 0–100;
- `Power` is the battery percentage (it dropped from 83 to 76 over the session);
- the BlueZ-negotiated MTU is 512, and no notification needed reassembly.

Behaviour to know about:

- **Powering the saber off from the hilt stops all Bluetooth activity.** It
  stops advertising and drops any connection, without sending a final state
  message. No client can turn it back on; it must be woken from the button.
- `IdleSleepTime` was 5 on this saber: the "auto deep sleep" delay, in minutes
  (1–60 in the app).

Confirmed on the same board: `ls "/"` (18 fonts in internal memory, no
`fontname` replies) and `df` (`[457, 479]` MiB).

Not yet tested on real hardware: the other file-system commands, file transfer,
firmware update and the `SoftwareType` / `HardwareType` changes. They follow
the app's code exactly and are tested against a simulated saber.
