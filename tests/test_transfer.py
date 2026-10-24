"""The YMODEM-variant transfer against the simulated saber in saber_files.py."""

import asyncio
import importlib
from pathlib import Path
import sys
import types

import pytest

_PKG_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "xeno_saber_ble"
_pkg = types.ModuleType("_xsb_standalone")
_pkg.__path__ = [str(_PKG_DIR)]
sys.modules.setdefault("_xsb_standalone", _pkg)
t = importlib.import_module("_xsb_standalone.transfer")
sys.path.insert(0, str(Path(__file__).resolve().parent))
from saber_files import SaberFiles  # noqa: E402


class Link:
    """Connects the transfer code to a SaberFiles instance."""

    def __init__(self, sim: SaberFiles) -> None:
        self.sim = sim
        self.queue: asyncio.Queue[bytes] = asyncio.Queue()
        self.sent: list[bytes] = []

    async def send(self, packet: bytes) -> None:
        self.sent.append(packet)
        for reply in self.sim.handle(packet):
            self.queue.put_nowait(reply)

    async def receive(self, timeout: float) -> bytes:
        return await asyncio.wait_for(self.queue.get(), timeout)


def run(coro):
    return asyncio.run(coro)


def test_crc_matches_crc16_xmodem() -> None:
    assert t.crc16_xmodem(b"123456789") == 0x31C3
    assert t.crc16_xmodem(b"") == 0


def test_blocks_are_framed_like_the_app() -> None:
    header = t.header_block("/3/hum (1).wav", 1234)
    assert len(header) == 133 and header[:3] == b"\x01\x00\xff"
    assert header[3:].startswith(b"/3/hum (1).wav\x001234\x00")
    block = t.data_block(0x101, b"abc")
    assert len(block) == 505 and block[:3] == b"\x11\x01\xfe"
    assert block[6:503] == b"\x1a" * 497
    with pytest.raises(t.TransferError):
        t.header_block("x" * 130, 1)
    with pytest.raises(ValueError):
        t.data_block(1, bytes(501))
    with pytest.raises(ValueError):
        t.transfer_command("sideways", "x")
    assert t.transfer_command(t.TO_SABER, "update.bin") == (
        b'[2,{"FileTransfer":["download","update.bin"]}]'
    )


def test_parse_blocks_from_the_saber() -> None:
    assert t.parse_header_block(t.header_block("/setting/config.ini", 42)) == (
        "/setting/config.ini", 42
    )
    with pytest.raises(t.TransferError):
        t.parse_header_block(b"\x01\x00\x00" + bytes(130))
    with pytest.raises(t.TransferError):
        t.parse_header_block(b"\x01\x00\xff" + b"name\0" + bytes(125))
    seq, payload = t.parse_data_block(t.data_block(7, b"hi"))
    assert seq == 7 and payload.startswith(b"hi\x1a")
    bad = bytearray(t.data_block(7, b"hi"))
    bad[-1] ^= 0xFF  # CRC mismatch is logged, not fatal (the app ignores it too)
    assert t.parse_data_block(bytes(bad))[0] == 7
    bad[2] = 0
    with pytest.raises(t.TransferError):
        t.parse_data_block(bytes(bad))
    with pytest.raises(t.TransferError):
        t.parse_data_block(b"\x01" + bytes(504))


@pytest.mark.parametrize("size", [0, 1, 499, 500, 501, 1500, 70000])
def test_send_file_round_trip(size: int) -> None:
    sim = SaberFiles()
    link = Link(sim)
    data = bytes(i % 251 for i in range(size))
    progress = []
    run(t.send_file(link.send, link.receive, "/3/hum (1).wav", data,
                    progress=lambda done, total: progress.append((done, total))))
    assert sim.files["/3/hum (1).wav"] == data
    assert sim.headers == [("/3/hum (1).wav", size, "")]
    if size:
        assert progress[-1] == (size, size)
    # Block numbers wrap after 255 like a byte.
    blocks = [p for p in link.sent if p[:1] == b"\x11"]
    assert len(blocks) == -(-size // 500)


def test_send_file_with_custom_header_name_and_combined_ack() -> None:
    sim = SaberFiles()
    sim.ack_and_c_together = True
    link = Link(sim)
    run(t.send_file(link.send, link.receive, "update.bin", b"fw" * 300, header_name="update.bin"))
    assert sim.headers[0][0] == "update.bin" and sim.files["update.bin"] == b"fw" * 300


def test_send_file_resends_a_nakked_block() -> None:
    sim = SaberFiles()
    sim.nak_once = {2}
    link = Link(sim)
    run(t.send_file(link.send, link.receive, "/1/x.wav", bytes(1200)))
    assert sim.files["/1/x.wav"] == bytes(1200)
    assert sum(1 for p in link.sent if p[:2] == b"\x11\x02") == 2


def test_send_file_cancelled_by_saber() -> None:
    sim = SaberFiles()
    sim.cancel_at_block = 2
    link = Link(sim)
    with pytest.raises(t.TransferError, match="cancelled"):
        run(t.send_file(link.send, link.receive, "/1/x.wav", bytes(1200)))


def test_send_file_refused_by_saber() -> None:
    sim = SaberFiles()
    sim.reject_transfer = True
    link = Link(sim)
    with pytest.raises(t.TransferError, match="refused"):
        run(t.send_file(link.send, link.receive, "/1/x.wav", b"x"))


def test_send_file_times_out(monkeypatch) -> None:
    monkeypatch.setattr(t, "REPLY_TIMEOUT", 0.01)
    monkeypatch.setattr(t, "PACKET_TIMEOUT", 0.01)
    sim = SaberFiles()
    sim.silent = True
    link = Link(sim)
    with pytest.raises(t.TransferError, match="did not answer"):
        run(t.send_file(link.send, link.receive, "/1/x.wav", b"x"))


class Scripted:
    """Replays fixed replies, for corner cases the simulation does not produce."""

    def __init__(self, replies: list[list[bytes]]) -> None:
        self.replies = replies
        self.queue: asyncio.Queue[bytes] = asyncio.Queue()
        self.sent: list[bytes] = []

    async def send(self, packet: bytes) -> None:
        self.sent.append(packet)
        for reply in self.replies.pop(0) if self.replies else []:
            self.queue.put_nowait(reply)

    async def receive(self, timeout: float) -> bytes:
        return await asyncio.wait_for(self.queue.get(), timeout)


def test_send_file_corner_cases(monkeypatch) -> None:
    monkeypatch.setattr(t, "PACKET_TIMEOUT", 0.01)
    ok = b'[3,{"FileTransfer":["download","f"]}]'
    # Header NAKked, then accepted (a long message containing "C" between ACK
    # and C is not taken for the C); 'C' noise around a block; first EOT
    # answered by ACK directly.
    link = Scripted([
        [ok], [b"\x15"], [ok, b"\x06", b'"CurrentClash"', b"C"], [b"C", b"\x06"], [b"\x06"],
    ])
    run(t.send_file(link.send, link.receive, "f", b"1"))
    # Header never acknowledged.
    link = Scripted([[b"C"], [b"\x06"], [], []])
    with pytest.raises(t.TransferError, match="file header"):
        run(t.send_file(link.send, link.receive, "f", b"1"))
    # Cancel right after the header.
    link = Scripted([[b"C"], [b"\x18"]])
    with pytest.raises(t.TransferError, match="cancelled"):
        run(t.send_file(link.send, link.receive, "f", b"1"))
    # Cancel instead of the first reply.
    link = Scripted([[b"\x18"]])
    with pytest.raises(t.TransferError, match="cancelled"):
        run(t.send_file(link.send, link.receive, "f", b"1"))
    # Block never acknowledged.
    link = Scripted([[b"C"], [b"\x06C"], [], [], []])
    with pytest.raises(t.TransferError, match="block 1"):
        run(t.send_file(link.send, link.receive, "f", b"1"))
    # End of file never confirmed, or cancelled at the end.
    link = Scripted([[b"C"], [b"\x06C"], [b"\x06"], [], [b"C"], []])
    with pytest.raises(t.TransferError, match="end of the file"):
        run(t.send_file(link.send, link.receive, "f", b"1"))
    link = Scripted([[b"C"], [b"\x06C"], [b"\x06"], [b"\x18"]])
    with pytest.raises(t.TransferError, match="cancelled"):
        run(t.send_file(link.send, link.receive, "f", b"1"))


@pytest.mark.parametrize("size", [1, 500, 1100, 70000])
@pytest.mark.parametrize("fragment", [None, 180])
def test_receive_file_round_trip(size: int, fragment) -> None:
    data = bytes((i * 7) % 256 for i in range(size))
    sim = SaberFiles({"config.ini": data})
    sim.fragment = fragment
    link = Link(sim)
    progress = []
    got = run(t.receive_file(link.send, link.receive, "config.ini",
                             progress=lambda d, n: progress.append((d, n))))
    assert got == data and progress[-1] == (size, size)
    assert sim.mode is None  # the saber saw the full EOT/NAK/EOT/ACK exchange


def test_receive_file_errors(monkeypatch) -> None:
    monkeypatch.setattr(t, "PACKET_TIMEOUT", 0.01)
    ok = b'[3,{"FileTransfer":["upload","f"]}]'
    header = t.header_block("f", 3)
    # Stray bytes and a repeated block are tolerated.
    block = t.data_block(1, b"abc")
    link = Scripted([[ok], [b"\x06" + header], [], [block, block], [], [b"\x04"], [b"\x04"], []])
    assert run(t.receive_file(link.send, link.receive, "f")) == b"abc"
    # The saber stops sending.
    link = Scripted([[ok], [header], [], []])
    with pytest.raises(t.TransferError, match="stopped sending"):
        run(t.receive_file(link.send, link.receive, "f"))
    link = Scripted([[ok], [header[:50]]])
    with pytest.raises(t.TransferError, match="mid-block"):
        run(t.receive_file(link.send, link.receive, "f"))
    link = Scripted([[ok], [b"\x18"]])
    with pytest.raises(t.TransferError, match="cancelled"):
        run(t.receive_file(link.send, link.receive, "f"))
    # Fewer bytes than announced.
    link = Scripted([[ok], [t.header_block("f", 900)], [], [block], [b"\x04"], [b"\x04"]])
    with pytest.raises(t.TransferError, match="Received 500 of 900"):
        run(t.receive_file(link.send, link.receive, "f"))
