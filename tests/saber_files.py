"""Simulated saber file system and transfer engine, written from the app's behaviour.

Independent of custom_components (own constants, CRC from binascii) so the
integration's transfer code is checked against a separate implementation.
"""

from __future__ import annotations

import binascii
import json

SOH, BLOCK, EOT, ACK, NAK, CAN, C = 0x01, 0x11, 0x04, 0x06, 0x15, 0x18, 0x43
SOUND_TYPES = (
    "begindrag",
    "beginlock",
    "beginmelt",
    "beginlightningblock",
    "blaster",
    "clash",
    "drag",
    "enddrag",
    "endlock",
    "endmelt",
    "endlightningblock",
    "font",
    "force",
    "hum",
    "in",
    "lock",
    "lightningblock",
    "melt",
    "out",
    "spin",
    "stab",
    "postoff",
    "preon",
    "sideout",
    "swing",
    "swingh",
    "swingl",
    "track",
)


def crc(data: bytes) -> bytes:
    return binascii.crc_hqx(data, 0).to_bytes(2, "big")


def _block(start: int, seq: int, payload: bytes) -> bytes:
    return bytes((start, seq & 0xFF, (seq & 0xFF) ^ 0xFF)) + payload + crc(payload)


def default_files() -> dict[str, bytes]:
    files = {"config.ini": b"volume=50\nend=1\n" * 70}  # ~1.1 kB: three blocks
    for font in (1, 2, 3):
        for name in (
            "hum (1).wav",
            "in (1).wav",
            "out (1).wav",
            *(f"clash ({i}).wav" for i in range(1, 5)),
        ):
            files[f"/{font}/{name}"] = b"RIFF" + bytes(40)
    return files


class SaberFiles:
    """Answers file-system commands and runs the saber side of transfers.

    ``handle(packet)`` returns the notifications the saber would send back.
    """

    def __init__(self, files: dict[str, bytes] | None = None) -> None:
        self.files = default_files() if files is None else dict(files)
        self.names = {1: "Vader", 2: "Luke", 3: "Obi-Wan"}
        self.mode: str | None = None
        self.path = ""
        self.size = 0
        self.header_name = ""
        self.received = bytearray()
        self.nak_once: set[int] = set()  # block numbers to NAK the first time
        self.cancel_at_block: int | None = None
        self.silent = False  # never answer (timeout tests)
        self.reject_transfer = False
        self.fragment: int | None = None  # split saber->phone blocks
        self.headers: list[tuple[str, int, str]] = []
        self.commands: list[dict[str, object]] = []
        self.ack_and_c_together = False

    # --------------------------------------------------------------- helpers
    def _json(self, payload: dict[str, object], kind: int = 3) -> bytes:
        return json.dumps([kind, payload], separators=(",", ":")).encode()

    def _split(self, data: bytes) -> list[bytes]:
        if not self.fragment:
            return [data]
        return [data[i : i + self.fragment] for i in range(0, len(data), self.fragment)]

    def ranges(self, font: int) -> list[int]:
        out: list[int] = []
        for sound_type in SOUND_TYPES:
            numbers = [
                int(path.rsplit("(", 1)[1].split(")")[0])
                for path in self.files
                if path.startswith(f"/{font}/{sound_type} (")
            ]
            out += [min(numbers), max(numbers)] if numbers else [0, 0]
        return out

    # ------------------------------------------------------------- dispatch
    def handle(self, packet: bytes) -> list[bytes]:
        if self.silent:
            return []
        if self.mode == "receive":
            return self._receive(packet)
        if self.mode == "send":
            return self._send(packet)
        payload = json.loads(packet)[1]
        self.commands.append(payload)
        if "FileTransfer" in payload:
            direction, path = payload["FileTransfer"]
            if self.reject_transfer:
                return [self._json({"FileTransfer": "error"}, kind=4)]
            self.path = path
            if direction == "download":  # the saber receives
                self.mode, self.received = "receive", bytearray()
                self.step = "header"
            else:
                if path not in self.files:
                    return []  # assumed: a missing file gets no answer (worst case)
                self.mode, self.step = "send", "start"
                self.outgoing_data = self.files[path]
            return [self._json({"FileTransfer": [direction, path]})]
        if "ls" in payload:
            if payload["ls"] == "/":
                fonts = sorted(self.names)
                replies = [self._json({"ls": [fonts[0], fonts[-1]]})]
                replies += [
                    self._json({"fontname": [f"/{n}", name]}) for n, name in self.names.items()
                ]
                return replies
            font = int(str(payload["ls"]).strip("/"))
            if font not in self.names:
                return [self._json({"ls": None})]
            return [self._json({"ls": self.ranges(font)})]
        if "df" in payload:
            return [self._json({"df": [1234, 30000]})]
        if "rm" in payload:
            existed = self.files.pop(str(payload["rm"]), None) is not None
            return [self._json({"rm": existed})]
        if "cp" in payload:
            src, dst = payload["cp"]  # type: ignore[misc]
            if src not in self.files:
                return [self._json({"cp": False})]
            self.files[dst] = self.files[src]
            return [self._json({"cp": True})]
        if "setname" in payload:
            folder, name = payload["setname"]  # type: ignore[misc]
            self.names[int(folder.strip("/"))] = name
            return [self._json({"setname": [folder, name]})]
        if "play" in payload:
            return [self._json({"play": 1})]
        return []  # plain settings: handled by FakeSaber

    # ---------------------------------------------- the saber receives a file
    def _receive(self, packet: bytes) -> list[bytes]:
        if self.step == "header":
            assert len(packet) == 133 and packet[:3] == bytes((SOH, 0, 0xFF)), packet[:8]
            assert packet[-2:] == crc(packet[3:131])
            name, size, md5 = packet[3:131].split(b"\0")[:3]
            self.header_name, self.size = name.decode(), int(size)
            self.headers.append((self.header_name, self.size, md5.decode()))
            self.step, self.expect = "data", 1
            if self.ack_and_c_together:
                return [bytes((ACK, C))]
            return [bytes((ACK,)), bytes((C,))]
        if self.step == "data" and packet == bytes((EOT,)):
            self.step = "eot"
            return [bytes((NAK,))]
        if self.step == "eot":
            assert packet == bytes((EOT,))
            self.files[self.path] = bytes(self.received[: self.size])
            self.mode = None
            return [bytes((ACK,))]
        assert len(packet) == 505 and packet[0] == BLOCK, (len(packet), packet[:3])
        seq = packet[1]
        assert packet[2] == seq ^ 0xFF and packet[-2:] == crc(packet[3:503])
        assert seq == self.expect & 0xFF, (seq, self.expect)
        if self.cancel_at_block == seq:
            self.mode = None
            return [bytes((CAN,))]
        if seq in self.nak_once:
            self.nak_once.discard(seq)
            return [bytes((NAK,))]
        self.received += packet[3:503]
        self.expect += 1
        return [bytes((ACK,))]

    # ------------------------------------------------- the saber sends a file
    def _send(self, packet: bytes) -> list[bytes]:
        code = packet[0]
        data = self.outgoing_data
        if self.step == "start":
            assert code == C
            header = f"/setting/{self.path}".encode() + b"\0" + str(len(data)).encode() + b"\0"
            self.step, self.seq = "header", 0
            return self._split(_block(SOH, 0, header.ljust(128, b"\0")))
        if self.step == "header":
            assert code == ACK
            self.step = "await_c"
            return []
        if self.step == "await_c":
            assert code == C
            self.step, self.seq = "data", 1
            return self._split(self._data_block())
        if self.step == "data":
            assert code == ACK
            if self.seq * 500 >= len(data):
                self.step = "eot1"
                return [bytes((EOT,))]
            self.seq += 1
            return self._split(self._data_block())
        if self.step == "eot1":
            assert code == NAK
            self.step = "eot2"
            return [bytes((EOT,))]
        assert self.step == "eot2" and code == ACK
        self.mode = None
        return []

    def _data_block(self) -> bytes:
        start = (self.seq - 1) * 500
        chunk = self.outgoing_data[start : start + 500].ljust(500, b"\x1a")
        return _block(BLOCK, self.seq, chunk)
