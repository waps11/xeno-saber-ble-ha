"""XenoSaber file-system commands and transfers, against the simulated saber."""

import asyncio
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from saber_files import SaberFiles  # noqa: E402
from test_device import FakeSaber, _BLEDevice, dev, p  # noqa: E402

_FILE_KEYS = (b'"FileTransfer"', b'"ls"', b'"df"', b'"rm"', b'"cp"', b'"setname"', b'"play"')


class FileSaber(FakeSaber):
    """FakeSaber plus the file system and transfer side of the firmware."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.mtu_size = 517
        self.sim = SaberFiles()
        self.push_state_during_transfer = False
        self.drop_on_transfer = False
        self.ignore_requests = 0  # FileTransfer commands to leave unanswered
        self.echo_on_dae1 = False  # answer the request on the handshake characteristic
        self.plain_echo = False  # answer with a state message without "FileTransfer"

    async def write_gatt_char(self, uuid, data, response=False):
        data = bytes(data)
        is_file = self.sim.mode is not None or not data.startswith(b"[") or any(
            k in data for k in _FILE_KEYS
        )
        if not is_file:
            return await super().write_gatt_char(uuid, data, response)
        assert response is False  # 3AB1 is write-without-response
        self.writes.append((uuid, data))
        if self.drop_on_transfer and b"FileTransfer" in data:
            self.is_connected = False
            self.on_disconnect(self)
            return None
        if b"FileTransfer" in data and self.sim.mode is None:
            if self.ignore_requests:
                self.ignore_requests -= 1
                return None
            replies = self.sim.handle(data)
            if self.plain_echo:
                replies = [b'[3,{"Power":81}]']
            target = p.HANDSHAKE_CHAR_UUID if self.echo_on_dae1 else uuid
            for reply in replies:
                self.notify[target](None, bytearray(reply))
            return None
        if self.push_state_during_transfer and data[:1] == b"\x11":
            self.push_state_during_transfer = False
            # On the data characteristic, whole; on DAE1, in 20-byte pieces
            # (some of which contain a "C") that must not reach the transfer.
            self.notify[uuid](None, bytearray(b'[3,{"Power":50}]'))
            self._push(p.HANDSHAKE_CHAR_UUID, [3, {"CurrentClash": 0, "Countdown": True}])
        for reply in self.sim.handle(data):
            self.notify[uuid](None, bytearray(reply))
        return None


@pytest.fixture
def files(monkeypatch):
    holder = {}

    async def fake_establish(cls, device, name, disconnected_callback=None, max_attempts=3):
        fw = FileSaber()
        if disconnected_callback:
            fw.on_disconnect = disconnected_callback
        holder["saber"] = fw
        return fw

    monkeypatch.setattr(dev, "establish_connection", fake_establish)
    monkeypatch.setattr(dev, "STATE_SETTLE", 0)
    monkeypatch.setattr(dev, "RETRY_DELAY", 0)
    monkeypatch.setattr(dev, "FONT_NAMES_WINDOW", 0.05)
    return holder


def test_file_system_commands(files, monkeypatch) -> None:
    async def go():
        saber = dev.XenoSaber(_BLEDevice())
        await saber.connect()
        assert await saber.list_fonts() == {1: "Vader", 2: "Luke", 3: "Obi-Wan"}
        # Replies about other fonts never overwrite the active font's name.
        assert saber.state["fontname"] == "Vader" and "ls" not in saber.state
        assert await saber.list_sounds(3) == {
            "clash": (1, 4), "hum": (1, 1), "in": (1, 1), "out": (1, 1),
        }
        assert await saber.list_sounds(9) == {}
        assert await saber.storage() == (1234, 30000)
        assert await saber.file_command(p.FILE_RM, "/3/clash (4).wav") is True
        assert "/3/clash (4).wav" not in files["saber"].sim.files
        assert files["saber"].writes[-1][1] == b'[2,{"rm":"/3/clash (4).wav"}]'

        # Names may not all arrive: unknown ones stay None.
        files["saber"].sim.names = {1: "Vader", 2: "Luke"}
        orig = files["saber"].sim.handle

        def no_names(packet):
            return [r for r in orig(packet) if b"fontname" not in r]

        files["saber"].sim.handle = no_names
        # Names learned earlier are remembered.
        assert await saber.list_fonts() == {1: "Vader", 2: "Luke"}
        saber.font_names.clear()
        assert await saber.list_fonts() == {1: None, 2: None}
        # The active font's name comes from the state (index 0-based).
        saber.state["CurrentSoundPackageNo"] = 1
        assert await saber.list_fonts() == {1: None, 2: "Vader"}
        files["saber"].sim.handle = orig

        monkeypatch.setattr(dev, "REPLY_TIMEOUT", 0.01)
        files["saber"].sim.silent = True
        with pytest.raises(dev.XenoSaberError, match="No reply"):
            await saber.storage()

    asyncio.run(go())


def test_unexpected_replies_raise(files, monkeypatch) -> None:
    async def go():
        saber = dev.XenoSaber(_BLEDevice())
        await saber.connect()
        sim = files["saber"].sim
        orig = sim.handle
        sim.handle = lambda packet: [b'[3,{"ls":[1,2,3]}]'] if b'"ls"' in packet else [b'[3,{"df":7}]']
        with pytest.raises(dev.XenoSaberError, match="Unexpected sound list"):
            await saber.list_sounds(1)
        with pytest.raises(dev.XenoSaberError, match="Unexpected storage"):
            await saber.storage()
        sim.handle = orig

    asyncio.run(go())


def test_send_and_receive_files(files) -> None:
    async def go():
        saber = dev.XenoSaber(_BLEDevice())
        seen = []
        saber.register_callback(
            lambda: seen.append(None if saber.transfer is None else saber.transfer.percent)
        )
        await saber.connect()
        fw = files["saber"]
        fw.push_state_during_transfer = True
        data = bytes(range(256)) * 20
        await saber.send_file("/2/hum (1).wav", data)
        assert fw.sim.files["/2/hum (1).wav"] == data
        assert saber.transfer is None and seen[-1] is None
        assert 100 in seen and 0 in seen  # progress was reported
        # State pushed mid-transfer still applied, on either characteristic.
        assert saber.state["Power"] == 50 and saber.state["Countdown"] is True

        assert await saber.receive_file("config.ini") == fw.sim.files["config.ini"]

        # Ordinary commands work again afterwards.
        await saber.send("Volume", 12)
        assert fw.state["Volume"] == 12

    asyncio.run(go())


def test_transfer_guards(files) -> None:
    async def go():
        saber = dev.XenoSaber(_BLEDevice())
        await saber.connect()
        fw = files["saber"]

        saber.transfer = dev.TransferStatus("download", "/1/x.wav", 50, 200)
        with pytest.raises(dev.XenoSaberBusy, match="25%"):
            await saber.send("Volume", 1)
        with pytest.raises(dev.XenoSaberBusy):
            await saber.reconnect()
        assert await saber.keepalive() is False
        saber.transfer = None
        assert dev.TransferStatus("upload", "x").percent == 0

        fw.mtu_size = 23
        with pytest.raises(dev.XenoSaberError, match="MTU of at least 508"):
            await saber.send_file("/1/x.wav", b"x")
        fw.mtu_size = 517

        fw.sim.cancel_at_block = 1
        with pytest.raises(dev.XenoSaberError, match="File transfer failed .*cancelled"):
            await saber.send_file("/1/x.wav", b"x")
        assert saber.transfer is None
        fw.sim.mode = None

        fw.drop_on_transfer = True
        with pytest.raises(dev.XenoSaberError, match="disconnected"):
            await saber.send_file("/1/x.wav", b"x")
        assert saber.transfer is None and not saber.is_connected

    asyncio.run(go())


def test_transfer_releases_saber_in_on_demand_mode(files) -> None:
    async def go():
        saber = dev.XenoSaber(_BLEDevice(), idle_disconnect=0.01)
        await saber.send_file("/1/x.wav", b"abc")
        assert files["saber"].sim.files["/1/x.wav"] == b"abc"
        await asyncio.sleep(0.05)
        assert not saber.is_connected

    asyncio.run(go())


@pytest.mark.parametrize("variant", ["echo_on_dae1", "plain_echo", "ignore_first"])
def test_transfer_request_answers_like_the_app(files, monkeypatch, variant) -> None:
    """The saber may answer on DAE1, without "FileTransfer", or only on a resend."""
    monkeypatch.setattr(dev.t, "REPLY_TIMEOUT", 0.05)

    async def go():
        saber = dev.XenoSaber(_BLEDevice())
        await saber.connect()
        fw = files["saber"]
        if variant == "ignore_first":
            fw.ignore_requests = 1
        else:
            setattr(fw, variant, True)
        assert await saber.receive_file("config.ini") == fw.sim.files["config.ini"]
        await saber.send_file("/1/x.wav", b"abc" * 300)
        assert fw.sim.files["/1/x.wav"] == b"abc" * 300
        if variant == "plain_echo":
            assert saber.state["Power"] == 81  # also applied as state

    asyncio.run(go())


def test_transfer_request_gives_up_after_three_attempts(files, monkeypatch) -> None:
    monkeypatch.setattr(dev.t, "REPLY_TIMEOUT", 0.01)

    async def go():
        saber = dev.XenoSaber(_BLEDevice())
        await saber.connect()
        files["saber"].ignore_requests = 5
        with pytest.raises(dev.XenoSaberError, match="3 attempts"):
            await saber.receive_file("config.ini")
        assert sum(b"FileTransfer" in w for _, w in files["saber"].writes) == 3

    asyncio.run(go())


def test_two_sabers_never_connect_at_the_same_time(monkeypatch) -> None:
    """With several sabers on one adapter, connection attempts are serialised."""
    active = {"now": 0, "max": 0}

    async def slow_establish(cls, device, name, disconnected_callback=None, max_attempts=3):
        active["now"] += 1
        active["max"] = max(active["max"], active["now"])
        await asyncio.sleep(0.02)
        active["now"] -= 1
        fw = FileSaber()
        if disconnected_callback:
            fw.on_disconnect = disconnected_callback
        return fw

    monkeypatch.setattr(dev, "establish_connection", slow_establish)
    monkeypatch.setattr(dev, "STATE_SETTLE", 0)

    class _Other(_BLEDevice):
        address = "11:22:33:44:55:66"

    async def go():
        one = dev.XenoSaber(_BLEDevice(), name="Saber EE:FF")
        two = dev.XenoSaber(_Other(), name="Saber 55:66")
        await asyncio.gather(one.connect(), two.connect())
        assert one.is_connected and two.is_connected
        assert one.name == "Saber EE:FF" and two.name == "Saber 55:66"

    asyncio.run(go())
    assert active["max"] == 1


def test_replies_survive_lost_fragments(files, monkeypatch) -> None:
    """A lost or garbled notification must not swallow the replies after it."""

    async def go():
        saber = dev.XenoSaber(_BLEDevice())
        await saber.connect()
        fw = files["saber"]
        # Half a state push whose end never arrives, then binary noise.
        fw.notify[p.DATA_CHAR_UUID](None, bytearray(b'[3,{"Volume":'))
        fw.notify[p.DATA_CHAR_UUID](None, bytearray(b"[\x01\x00\xff"))
        assert await saber.storage() == (1234, 30000)
        # A state push split on DAE1 while a reply arrives on 3AB1.
        fw.notify[p.HANDSHAKE_CHAR_UUID](None, bytearray(b'[3,{"Power":'))
        assert await saber.list_fonts() == {1: "Vader", 2: "Luke", 3: "Obi-Wan"}
        fw.notify[p.HANDSHAKE_CHAR_UUID](None, bytearray(b"42}]"))
        assert saber.state["Power"] == 42

        # A query whose reply is lost is asked again, once.
        monkeypatch.setattr(dev, "REPLY_TIMEOUT", 0.05)
        orig, lost = fw.sim.handle, {"n": 1}

        def lose_first(packet):
            replies = orig(packet)
            if b'"ls"' in packet and lost["n"]:
                lost["n"] -= 1
                return []
            return replies

        fw.sim.handle = lose_first
        assert await saber.list_sounds(3) == {
            "clash": (1, 4), "hum": (1, 1), "in": (1, 1), "out": (1, 1),
        }
        assert sum(b'"ls"' in w for _, w in fw.writes) >= 2
        # Changes are never sent twice.
        fw.sim.silent = True
        before = len(fw.writes)
        with pytest.raises(dev.XenoSaberError, match="No reply"):
            await saber.file_command(p.FILE_RM, "/3/clash (1).wav", timeout=0.05)
        assert len(fw.writes) == before + 1

    asyncio.run(go())
