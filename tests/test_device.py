"""End-to-end test of XenoSaber against a simulated saber (behaviour from the decompiled app)."""

import asyncio
import importlib
import json
from pathlib import Path
import sys
import types

import pytest

_PKG_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "xeno_saber_ble"
_pkg = types.ModuleType("_xsb_standalone")
_pkg.__path__ = [str(_PKG_DIR)]
sys.modules.setdefault("_xsb_standalone", _pkg)
p = importlib.import_module("_xsb_standalone.protocol")
dev = importlib.import_module("_xsb_standalone.device")


class _Char:
    def __init__(self, uuid, props):
        self.uuid, self.properties = uuid, props


class _Services:
    def __init__(self, chars):
        self._c = {c.uuid: c for c in chars}

    def get_characteristic(self, uuid):
        return self._c.get(uuid)


class FakeSaber:
    """Simulates the saber firmware. Notifications are fragmented at 20 bytes."""

    def __init__(self, with_handshake=True):
        self.state = {
            "HardwareVersion": "XENO3", "SoftwareVersion": "1.0", "PowerOn": False,
            "Power": 82, "Volume": 40, "Brightness": 100, "BackgroundColor": [0, 0, 255],
            "CurrentSoundPackageNo": 2, "TotalSoundPackage": 34, "fontname": "Vader",
        }
        # GATT flags as reported by a real board (firmware DMN_XENO_B_SV1.4.0).
        chars = [_Char(p.DATA_CHAR_UUID, ["read", "write-without-response", "notify"])]
        if with_handshake:
            chars.append(_Char(p.HANDSHAKE_CHAR_UUID, ["read", "write", "notify"]))
        self.services = _Services(chars)
        self.is_connected = True
        self.mtu_size = 23
        self.notify = {}
        self.writes = []
        self.authorized = False
        self.drop_during_handshake = False
        self.on_disconnect = lambda client: None

    async def start_notify(self, uuid, cb):
        self.notify[uuid] = cb

    def _push(self, uuid, obj):
        raw = json.dumps(obj, separators=(",", ":")).encode()
        for i in range(0, len(raw), 20):
            self.notify[uuid](None, bytearray(raw[i : i + 20]))

    async def write_gatt_char(self, uuid, data, response=False):
        # Must match the characteristic: DAE1 = write, 3AB1 = write-without-response.
        assert response is (uuid == p.HANDSHAKE_CHAR_UUID)
        self.writes.append((uuid, bytes(data)))
        typ, payload = json.loads(data)
        assert typ == 2
        if "HandShake" in payload:
            assert uuid == p.HANDSHAKE_CHAR_UUID and payload["HandShake"] == "HelloDamien"
            if self.drop_during_handshake:
                self.drop_during_handshake = False
                self.is_connected = False
                self.on_disconnect(self)
                return
            # Real board: state dump in several notifications, "HandShake":"OK" last.
            items = list(self.state.items())
            self._push(uuid, [3, dict(items[: len(items) // 2])])
            self._push(uuid, [3, {**dict(items[len(items) // 2 :]), "HandShake": "OK"}])
            return
        if "Authorize" in payload:
            assert uuid == p.DATA_CHAR_UUID
            self.authorized = payload["Authorize"] == "SaberOfDamien"
            self._push(uuid, [3, {"Authorize": "AccessAllowed"}])
            return
        assert uuid == p.DATA_CHAR_UUID and self.authorized
        self.state.update(payload)
        self._push(uuid, [3, payload])

    async def read_gatt_char(self, uuid):
        return f'[3,{{"Authorize":"{"AccessAllowed" if self.authorized else "AccessDenied"}"}}]'.encode()

    async def disconnect(self):
        self.is_connected = False


class _BLEDevice:
    address = "AA:BB:CC:DD:EE:FF"
    name = "Xeno3"


@pytest.fixture
def fake(monkeypatch):
    holder = {}

    async def fake_establish(cls, device, name, disconnected_callback=None, max_attempts=3):
        fw = FakeSaber(holder.get("with_handshake", True))
        holder.setdefault("all", []).append(fw)
        if holder.pop("drop_next", False):
            fw.drop_during_handshake = True
        if disconnected_callback:
            fw.on_disconnect = disconnected_callback
        holder["saber"] = fw
        return fw

    monkeypatch.setattr(dev, "establish_connection", fake_establish)
    monkeypatch.setattr(dev, "STATE_SETTLE", 0)
    monkeypatch.setattr(dev, "RETRY_DELAY", 0)
    return holder


def test_connect_handshake_authorize_and_state(fake):
    async def go():
        saber = dev.XenoSaber(_BLEDevice())
        calls = []
        saber.register_callback(lambda: calls.append(1))
        await saber.connect()
        fw = fake["saber"]
        assert fw.writes[0] == (p.HANDSHAKE_CHAR_UUID, b'[2,{"HandShake":"HelloDamien"}]')
        assert fw.writes[1] == (p.DATA_CHAR_UUID, b'[2,{"Authorize":"SaberOfDamien"}]')
        assert saber.authorized is True
        assert saber.battery == 82
        assert saber.blade_on is False
        assert saber.color == (0, 0, 255)
        assert saber.state["fontname"] == "Vader"
        assert "HandShake" not in saber.state
        assert calls

        await saber.set_blade(True)
        assert fw.writes[-1] == (p.DATA_CHAR_UUID, b'[2,{"PowerOn":true}]')
        assert saber.blade_on is True and fw.state["PowerOn"] is True

        await saber.set_color(255, 10, 0)
        assert fw.writes[-1][1] == b'[2,{"BackgroundColor":[255,10,0]}]'
        assert saber.color == (255, 10, 0)

        await saber.send("CurrentSoundPackageNo", 5)
        assert saber.state["CurrentSoundPackageNo"] == 5
        await saber.disconnect()
        assert not saber.is_connected

    asyncio.run(go())


def test_works_without_handshake_service(fake):
    fake["with_handshake"] = False

    async def go():
        saber = dev.XenoSaber(_BLEDevice())
        await saber.connect()
        assert fake["saber"].writes[0][1] == b'[2,{"Authorize":"SaberOfDamien"}]'
        await saber.set_blade(False)
        assert saber.blade_on is False

    asyncio.run(go())


def test_reconnects_on_demand_after_disconnect(fake):
    async def go():
        saber = dev.XenoSaber(_BLEDevice(), idle_disconnect=0.05)
        await saber.set_blade(True)  # connects on demand
        first = fake["saber"]
        assert saber.is_connected
        await asyncio.sleep(0.15)  # idle timer fires
        assert not saber.is_connected and not first.is_connected
        await saber.set_blade(False)  # reconnects
        assert fake["saber"] is not first and saber.blade_on is False
        await saber.disconnect()

    asyncio.run(go())


def test_retries_when_link_drops_during_handshake(fake):
    """Seen on real hardware at weak RSSI: the saber drops mid-handshake."""
    fake["drop_next"] = True

    async def go():
        saber = dev.XenoSaber(_BLEDevice())
        t0 = asyncio.get_running_loop().time()
        await saber.connect()
        elapsed = asyncio.get_running_loop().time() - t0
        assert len(fake["all"]) == 2  # first attempt dropped, second succeeded
        assert elapsed < dev.HANDSHAKE_TIMEOUT  # failed fast, no 5 s wait
        assert saber.is_connected and saber.authorized and saber.battery == 82
        await saber.disconnect()

    asyncio.run(go())


def test_gives_up_after_max_attempts(fake, monkeypatch):
    async def always_drop(cls, device, name, disconnected_callback=None, max_attempts=3):
        fw = FakeSaber()
        fw.drop_during_handshake = True
        fw.on_disconnect = disconnected_callback
        fake.setdefault("all", []).append(fw)
        return fw

    monkeypatch.setattr(dev, "establish_connection", always_drop)

    async def go():
        saber = dev.XenoSaber(_BLEDevice())
        with pytest.raises(dev.XenoSaberError, match="disconnected during handshake"):
            await saber.connect()
        assert len(fake["all"]) == dev.SESSION_ATTEMPTS

    asyncio.run(go())


def test_unreachable_saber_is_not_retried_by_session_loop(fake, monkeypatch):
    """A powered-off saber: establish_connection already retried, so fail once."""
    calls = []

    async def unreachable(cls, device, name, disconnected_callback=None, max_attempts=3):
        calls.append(1)
        raise dev.BleakError("Device with address AA:BB:CC:DD:EE:FF was not found")

    monkeypatch.setattr(dev, "establish_connection", unreachable)

    async def go():
        with pytest.raises(dev.XenoSaberUnreachable):
            await dev.XenoSaber(_BLEDevice()).connect()
        assert len(calls) == 1

    asyncio.run(go())


def test_keepalive_resends_current_brightness_only_when_connected(fake):
    async def go():
        saber = dev.XenoSaber(_BLEDevice())
        assert await saber.keepalive() is False  # not connected: no reconnect
        assert "saber" not in fake
        await saber.connect()
        fw = fake["saber"]
        assert await saber.keepalive() is True
        assert fw.writes[-1] == (p.DATA_CHAR_UUID, b'[2,{"Brightness":100}]')
        await saber.send(p.KEY_BRIGHTNESS, 40)
        assert await saber.keepalive() is True
        assert fw.writes[-1][1] == b'[2,{"Brightness":40}]'
        await saber.disconnect()
        writes = len(fw.writes)
        assert await saber.keepalive() is False
        assert len(fake["all"]) == 1 and len(fw.writes) == writes

    asyncio.run(go())


def test_keepalive_skips_when_brightness_unknown(fake):
    async def go():
        saber = dev.XenoSaber(_BLEDevice())
        await saber.connect()
        saber.state.pop(p.KEY_BRIGHTNESS)
        writes = len(fake["saber"].writes)
        assert await saber.keepalive() is False
        assert len(fake["saber"].writes) == writes

    asyncio.run(go())


def test_reconnect_opens_a_fresh_session(fake):
    async def go():
        saber = dev.XenoSaber(_BLEDevice())
        await saber.connect()
        first = fake["saber"]
        await saber.reconnect()
        assert not first.is_connected
        assert fake["saber"] is not first and saber.is_connected and saber.authorized
        await saber.disconnect()

    asyncio.run(go())


def test_lockup_and_drag_are_echoed_into_state(fake):
    async def go():
        saber = dev.XenoSaber(_BLEDevice())
        await saber.connect()
        await saber.send(p.KEY_LOCKUP, True)
        assert fake["saber"].writes[-1][1] == b'[2,{"Lockup":true}]'
        await saber.send(p.KEY_DRAG, False)
        assert saber.state[p.KEY_LOCKUP] is True and saber.state[p.KEY_DRAG] is False

    asyncio.run(go())


def test_reconnect_in_on_demand_mode_still_releases_the_saber(fake):
    async def go():
        saber = dev.XenoSaber(_BLEDevice(), idle_disconnect=0.05)
        await saber.reconnect()
        assert saber.is_connected
        await asyncio.sleep(0.15)  # idle timer must have been armed
        assert not saber.is_connected

    asyncio.run(go())


# ------------------------------------------------------------ edge cases
def _run(coro):
    return asyncio.run(coro)


def test_listener_errors_are_contained_and_ble_device_can_change(fake, caplog):
    async def go():
        saber = dev.XenoSaber(_BLEDevice())

        def boom():
            raise RuntimeError("listener bug")

        saber.register_callback(boom)
        await saber.connect()  # notifies listeners; must not raise
        assert "Error in Xeno Saber BLE listener" in caplog.text

        class Moved:
            address = "AA:BB:CC:DD:EE:FF"
            name = None

        saber.set_ble_device(Moved())
        assert saber.name == "Xeno saber"

    _run(go())


def test_plain_bleak_client_is_used_without_retry_connector(fake, monkeypatch):
    made = []

    class Client(FakeSaber):
        def __init__(self, device, disconnected_callback=None):
            super().__init__()
            self.is_connected = False
            self.on_disconnect = disconnected_callback
            made.append(self)

        async def connect(self):
            self.is_connected = True

    monkeypatch.setattr(dev, "establish_connection", None)
    monkeypatch.setattr(dev, "BleakClient", Client)

    async def go():
        saber = dev.XenoSaber(_BLEDevice())
        await saber.connect()
        assert len(made) == 1 and saber.is_connected and saber.authorized

    _run(go())


def test_missing_handshake_reply_times_out_and_continues(fake, monkeypatch):
    class Silent(FakeSaber):
        async def write_gatt_char(self, uuid, data, response=False):
            if b"HandShake" in data:
                self.writes.append((uuid, bytes(data)))
                return  # no reply at all
            await super().write_gatt_char(uuid, data, response)

    async def establish(cls, device, name, disconnected_callback=None, max_attempts=3):
        return Silent()

    monkeypatch.setattr(dev, "establish_connection", establish)
    monkeypatch.setattr(dev, "HANDSHAKE_TIMEOUT", 0.01)

    async def go():
        saber = dev.XenoSaber(_BLEDevice())
        await saber.connect()
        assert saber.is_connected and saber.authorized

    _run(go())


def test_mtu_lookup_is_best_effort(fake, monkeypatch):
    class Odd(FakeSaber):
        def __init__(self):
            super().__init__()

            async def broken():
                raise RuntimeError("no D-Bus")

            self._backend = types.SimpleNamespace(_acquire_mtu=broken)

    class NoMtu(FakeSaber):
        @property
        def mtu_size(self):
            raise RuntimeError("not negotiated")

        @mtu_size.setter
        def mtu_size(self, value):
            pass

    for cls in (Odd, NoMtu):
        async def establish(c, device, name, disconnected_callback=None, max_attempts=3, _cls=cls):
            return _cls()

        monkeypatch.setattr(dev, "establish_connection", establish)
        _run(dev.XenoSaber(_BLEDevice()).connect())  # must not raise


def test_not_a_xeno_board_is_rejected(fake, monkeypatch):
    class Other(FakeSaber):
        def __init__(self):
            super().__init__()
            self.services = _Services([_Char("0000ffe1-0000-1000-8000-00805f9b34fb", ["notify"])])

    async def establish(cls, device, name, disconnected_callback=None, max_attempts=3):
        return Other()

    monkeypatch.setattr(dev, "establish_connection", establish)
    with pytest.raises(dev.XenoSaberError, match="not a Xeno V3"):
        _run(dev.XenoSaber(_BLEDevice()).connect())


def test_gatt_error_during_session_setup(fake, monkeypatch):
    class Broken(FakeSaber):
        async def start_notify(self, uuid, cb):
            raise dev.BleakError("CCCD write not permitted")

    async def establish(cls, device, name, disconnected_callback=None, max_attempts=3):
        return Broken()

    monkeypatch.setattr(dev, "establish_connection", establish)
    with pytest.raises(dev.XenoSaberError, match="Session setup failed"):
        _run(dev.XenoSaber(_BLEDevice()).connect())


def test_denied_authorization_is_logged_but_not_fatal(fake, monkeypatch, caplog):
    class Denies(FakeSaber):
        async def read_gatt_char(self, uuid):
            return b'[3,{"Authorize":"AccessDenied"}]'

    async def establish(cls, device, name, disconnected_callback=None, max_attempts=3):
        return Denies()

    monkeypatch.setattr(dev, "establish_connection", establish)

    async def go():
        saber = dev.XenoSaber(_BLEDevice())
        await saber.connect()
        assert saber.is_connected and saber.authorized is False
        assert "continuing anyway" in caplog.text

    _run(go())


def test_disconnect_errors_and_stale_callbacks_are_ignored(fake):
    async def go():
        saber = dev.XenoSaber(_BLEDevice())
        await saber.connect()
        fw = fake["saber"]
        saber._on_disconnect(object())  # late callback from an old client
        assert saber.is_connected

        async def broken_disconnect():
            raise dev.BleakError("already gone")

        fw.disconnect = broken_disconnect
        await saber.disconnect()  # must not raise
        assert not saber.is_connected

    _run(go())


def test_error_messages_from_the_saber_are_logged(fake, caplog):
    async def go():
        saber = dev.XenoSaber(_BLEDevice())
        await saber.connect()
        fake["saber"]._push(p.DATA_CHAR_UUID, [4, {"Volume": "out of range"}])
        assert "saber reported error" in caplog.text

    _run(go())


def test_keepalive_write_failure_returns_false(fake):
    async def go():
        saber = dev.XenoSaber(_BLEDevice())
        await saber.connect()

        async def broken(*a, **k):
            raise dev.BleakError("GATT 0x85")

        fake["saber"].write_gatt_char = broken
        assert await saber.keepalive() is False

    _run(go())


def test_connection_loss_and_recovery_are_logged_once_each(fake, caplog):
    import logging

    caplog.set_level(logging.INFO, logger=dev.__name__)

    async def go():
        saber = dev.XenoSaber(_BLEDevice())
        await saber.connect()
        for _ in range(3):  # repeated drops while the saber is away: one message
            fw = fake["saber"]
            fw.is_connected = False
            fw.on_disconnect(fw)
            saber._client = None
        lost = [r for r in caplog.records if "connection lost" in r.getMessage()]
        assert len(lost) == 1 and lost[0].levelno == logging.INFO
        await saber.connect()
        restored = [r for r in caplog.records if "connection restored" in r.getMessage()]
        assert len(restored) == 1
        # A deliberate disconnect (idle timer, unload) is not reported as a loss.
        await saber.disconnect()
        assert len([r for r in caplog.records if "connection lost" in r.getMessage()]) == 1

    _run(go())
