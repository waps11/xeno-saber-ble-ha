#!/usr/bin/env python3
"""Test a Xenopixel V3 compatible saber from a laptop, without Home Assistant.

    pip install bleak
    python tools/xeno_cli.py scan
    python tools/xeno_cli.py state  AA:BB:CC:DD:EE:FF
    python tools/xeno_cli.py on     AA:BB:CC:DD:EE:FF
    python tools/xeno_cli.py off    AA:BB:CC:DD:EE:FF
    python tools/xeno_cli.py color  AA:BB:CC:DD:EE:FF 255 0 0
    python tools/xeno_cli.py set    AA:BB:CC:DD:EE:FF Volume 60
    python tools/xeno_cli.py raw    AA:BB:CC:DD:EE:FF '[2,{"Clash":true}]'
    python tools/xeno_cli.py listen AA:BB:CC:DD:EE:FF
    python tools/xeno_cli.py fonts  AA:BB:CC:DD:EE:FF            # font folders, names, card usage
    python tools/xeno_cli.py sounds AA:BB:CC:DD:EE:FF 3          # files of font 3
    python tools/xeno_cli.py get    AA:BB:CC:DD:EE:FF config.ini backup.ini
    python tools/xeno_cli.py put    AA:BB:CC:DD:EE:FF backup.ini config.ini

`get` only reads from the saber: it is the safe first test of file transfer.

Reuses the integration's protocol.py and device.py so this is also a test of
the integration code itself.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib
import json
import logging
from pathlib import Path
import sys
import types

from bleak import BleakScanner
from bleak.exc import BleakError

# Load custom_components/xeno_saber_ble/{protocol,device}.py without running the
# package __init__ (which imports Home Assistant).
_PKG_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "xeno_saber_ble"
_pkg = types.ModuleType("_xsb_standalone")
_pkg.__path__ = [str(_PKG_DIR)]
sys.modules["_xsb_standalone"] = _pkg
p = importlib.import_module("_xsb_standalone.protocol")
XenoSaber = importlib.import_module("_xsb_standalone.device").XenoSaber


BUSY_HINT = (
    "BlueZ reports another scan already in progress on this adapter. Close the "
    "GNOME/KDE Bluetooth settings panel or Blueman, stop any `bluetoothctl scan on`, "
    "then retry. If it persists: `bluetoothctl scan off` or "
    "`sudo systemctl restart bluetooth`."
)


async def _discover(timeout: float, adapter: str | None, stop_on: str | None = None):
    """Scan and return {address: (device, adv)}.

    Unlike BleakScanner.discover(), a BlueZ error while *stopping* the scan does
    not throw away the results already collected. Starting is retried a few
    times because BlueZ answers "InProgress" while another scan is winding down.
    """
    found: dict = {}
    done = asyncio.Event()

    def on_adv(device, adv):
        found[device.address] = (device, adv)
        if stop_on and device.address.upper() == stop_on.upper():
            done.set()

    kwargs = {"detection_callback": on_adv}
    if adapter:
        kwargs["adapter"] = adapter
    scanner = BleakScanner(**kwargs)

    for attempt in range(3):
        try:
            await scanner.start()
            break
        except BleakError as err:
            if "InProgress" not in str(err) or attempt == 2:
                sys.exit(f"Could not start scanning: {err}\n{BUSY_HINT}")
            await asyncio.sleep(2)
    try:
        await asyncio.wait_for(done.wait(), timeout)
    except TimeoutError:
        pass
    finally:
        try:
            await scanner.stop()
        except BleakError as err:
            logging.getLogger(__name__).warning("Ignoring error while stopping scan: %s", err)
    return found


async def scan(timeout: float, adapter: str | None, show_all: bool) -> None:
    wanted = {p.DATA_SERVICE_UUID, p.HANDSHAKE_SERVICE_UUID}
    found = await _discover(timeout, adapter)
    hits = 0
    for device, adv in sorted(found.values(), key=lambda da: -(da[1].rssi or -999)):
        uuids = {u.lower() for u in adv.service_uuids}
        match = uuids & wanted
        if match:
            hits += 1
        if match or (show_all and (device.name or adv.local_name)):
            tag = "SABER" if match else "     "
            print(
                f"{tag} {device.address}  rssi={adv.rssi:>4}  {device.name or adv.local_name or '?'}"
                f"  uuids={sorted(uuids)}"
            )
    if not hits:
        print(
            f"No saber found among {len(found)} devices. Wake it up (hold the button), "
            "close the phone app, or rerun with --all to list every named device."
        )


async def _connect(address: str, adapter: str | None) -> XenoSaber:
    found = await _discover(15, adapter, stop_on=address)
    match = next((d for a, (d, _) in found.items() if a.upper() == address.upper()), None)
    if match is None:
        sys.exit(f"{address} not found. Is it awake and is the phone app closed?")
    saber = XenoSaber(match)
    await saber.connect()
    print(f"connected, authorized={saber.authorized}")
    return saber


def _parse_value(text: str):
    try:
        return json.loads(text)
    except ValueError:
        return text


async def run(args: argparse.Namespace) -> None:
    if args.cmd == "scan":
        await scan(args.timeout, args.adapter, args.all)
        return

    saber = await _connect(args.address, args.adapter)
    try:
        if args.cmd == "on":
            await saber.set_blade(True)
        elif args.cmd == "off":
            await saber.set_blade(False)
        elif args.cmd == "color":
            await saber.set_color(args.r, args.g, args.b)
        elif args.cmd == "set":
            await saber.send(args.key, _parse_value(args.value))
        elif args.cmd == "raw":
            await saber.send_raw(args.payload.encode())
        elif args.cmd == "fonts":
            fonts = await saber.list_fonts()
            used, total = await saber.storage()
            print(json.dumps({"fonts": fonts, "used": used, "total": total}, indent=2))
            return
        elif args.cmd == "sounds":
            print(json.dumps(await saber.list_sounds(args.font), indent=2))
            return
        elif args.cmd == "get":
            data = await saber.receive_file(args.source)
            Path(args.destination).write_bytes(data)
            print(f"{len(data)} bytes written to {args.destination}")
            return
        elif args.cmd == "put":
            data = Path(args.source).read_bytes()
            await saber.send_file(args.destination, data)
            print(f"{len(data)} bytes sent to {args.destination}")
            return
        elif args.cmd == "listen":
            saber.register_callback(lambda: print(json.dumps(saber.state, ensure_ascii=False)))
            print("listening, Ctrl-C to stop")
            while True:
                await asyncio.sleep(3600)
        await asyncio.sleep(1.0)  # collect the state echo
        print(json.dumps(saber.state, indent=2, ensure_ascii=False, sort_keys=True))
    finally:
        await saber.disconnect()


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--adapter", help="Bluetooth adapter to use, e.g. hci1 (Linux only)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan")
    s.add_argument("--timeout", type=float, default=8)
    s.add_argument("--all", action="store_true", help="also list non-saber named devices")
    for name in ("state", "on", "off", "listen"):
        sub.add_parser(name).add_argument("address")
    c = sub.add_parser("color")
    c.add_argument("address")
    for ch in "rgb":
        c.add_argument(ch, type=int)
    st = sub.add_parser("set")
    st.add_argument("address")
    st.add_argument("key")
    st.add_argument("value", help='JSON literal: true, 42, "text", [1,2,3]')
    r = sub.add_parser("raw")
    r.add_argument("address")
    r.add_argument("payload")
    sub.add_parser("fonts").add_argument("address")
    so = sub.add_parser("sounds")
    so.add_argument("address")
    so.add_argument("font", type=int)
    for name, src, dst in (
        ("get", "path on the saber", "local file"),
        ("put", "local file", "path on the saber"),
    ):
        g = sub.add_parser(name)
        g.add_argument("address")
        g.add_argument("source", help=src)
        g.add_argument("destination", help=dst)
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run(args))


if __name__ == "__main__":
    main()
