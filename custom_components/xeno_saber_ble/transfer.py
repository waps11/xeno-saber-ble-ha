"""File transfer with the saber: a YMODEM variant over the data characteristic.

Pure asyncio, no Bluetooth or Home Assistant imports, so it can be tested
against a simulated saber. The caller supplies ``send`` (write one packet) and
``receive`` (wait for the next notification, raising ``TimeoutError``).

How the saber's variant differs from textbook YMODEM:

* the transfer starts with a JSON command, ``[2,{"FileTransfer":[dir, path]}]``,
  where ``dir`` is seen from the saber: "download" sends a file *to* it,
  "upload" reads one *from* it;
* the header block is the usual 133 bytes (SOH, 00, FF, 128 bytes, CRC) and
  carries ``name NUL size NUL md5`` (the official app leaves md5 empty);
* data blocks start with 0x11 and carry 500 bytes (505 with framing), so they
  need an ATT MTU of at least 508; the last one is padded with 0x1A;
* the sender ends with EOT, the receiver answers NAK, the sender repeats EOT
  and the receiver answers ACK. There is no closing empty header.

CRCs are CRC-16/XMODEM (poly 0x1021, init 0), big-endian.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import json
import logging
import re

from . import protocol as p

_LOGGER = logging.getLogger(__name__)

SOH = 0x01
BLOCK_START = 0x11
EOT = 0x04
ACK = 0x06
NAK = 0x15
CAN = 0x18
CRC_REQUEST = 0x43  # 'C'
PAD = 0x1A

CONTROL_BYTES = frozenset({SOH, EOT, ACK, NAK, CAN, CRC_REQUEST})

HEADER_PAYLOAD = 128
HEADER_LEN = 3 + HEADER_PAYLOAD + 2
BLOCK_PAYLOAD = 500
BLOCK_LEN = 3 + BLOCK_PAYLOAD + 2
MIN_MTU = BLOCK_LEN + 3  # ATT header

# Control replies are one byte (two if ACK and C arrive together); anything
# longer is a state message or noise, never a verdict on our packet.
_CONTROL_MAX = 2

TO_SABER = "download"
FROM_SABER = "upload"

REPLY_TIMEOUT = 5.0  # first answer to the JSON command, per attempt
REQUEST_ATTEMPTS = 3  # the app resends the request when the saber stays silent
PACKET_TIMEOUT = 10.0  # what the official app waits for each packet
RETRIES = 3

Send = Callable[[bytes], Awaitable[None]]
Receive = Callable[[float], Awaitable[bytes]]
Progress = Callable[[int, int], None]


class TransferError(Exception):
    """The transfer failed or the saber refused it."""


def crc16_xmodem(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else crc << 1
            crc &= 0xFFFF
    return crc


def _frame(start: int, seq: int, payload: bytes) -> bytes:
    seq &= 0xFF
    return (
        bytes((start, seq, seq ^ 0xFF))
        + payload
        + crc16_xmodem(payload).to_bytes(2, "big")
    )


def transfer_command(direction: str, path: str) -> bytes:
    if direction not in (TO_SABER, FROM_SABER):
        raise ValueError(f"Unknown direction: {direction!r}")
    return p.encode_command(p.FILE_TRANSFER, [direction, path])


def header_block(name: str, size: int, md5: str = "") -> bytes:
    fields = name.encode() + b"\0" + str(size).encode() + b"\0" + md5.encode()
    if len(fields) > HEADER_PAYLOAD:
        raise TransferError(f"File name too long for the transfer header: {name!r}")
    return _frame(SOH, 0, fields.ljust(HEADER_PAYLOAD, b"\0"))


def data_block(seq: int, chunk: bytes) -> bytes:
    if len(chunk) > BLOCK_PAYLOAD:
        raise ValueError("Chunk larger than a block")
    return _frame(BLOCK_START, seq, chunk.ljust(BLOCK_PAYLOAD, bytes((PAD,))))


def parse_header_block(frame: bytes) -> tuple[str, int]:
    """Return (name, size) from a header block sent by the saber."""
    if len(frame) < HEADER_LEN or frame[:3] != bytes((SOH, 0x00, 0xFF)):
        raise TransferError("Invalid file header from the saber")
    fields = frame[3 : 3 + HEADER_PAYLOAD].split(b"\0")
    name = fields[0].decode("utf-8", errors="replace")
    size_field = fields[1].decode("ascii", errors="replace") if len(fields) > 1 else ""
    match = re.match(r"\s*(\d+)", size_field)
    if match is None:
        raise TransferError(f"No file size in the header from the saber ({name!r})")
    return name, int(match.group(1))


def parse_data_block(frame: bytes) -> tuple[int, bytes]:
    """Return (sequence number, 500-byte payload) from a data block."""
    if len(frame) < BLOCK_LEN or frame[0] != BLOCK_START:
        raise TransferError("Invalid data block from the saber")
    seq, inverse = frame[1], frame[2]
    payload = frame[3 : 3 + BLOCK_PAYLOAD]
    if seq ^ inverse != 0xFF:
        raise TransferError("Corrupted block number from the saber")
    crc = int.from_bytes(frame[3 + BLOCK_PAYLOAD : BLOCK_LEN], "big")
    if crc != crc16_xmodem(payload):
        # The official app does not check it either; the link layer already does.
        _LOGGER.debug("CRC mismatch on block %d (ignored)", seq)
    return seq, payload


def _json_reply(item: bytes) -> tuple[int, dict[str, object]] | None:
    if not item.startswith(b"["):
        return None
    try:
        data = json.loads(item.decode("utf-8", errors="replace").strip("\x00 \r\n"))
    except ValueError:
        return None
    if isinstance(data, list) and len(data) >= 2 and isinstance(data[1], dict):
        return int(data[0]), data[1]
    return None


async def _request(
    send: Send, receive: Receive, command: bytes, attempts: int = REQUEST_ATTEMPTS
) -> bytes:
    """Send the FileTransfer command and wait for the saber's first answer.

    Like the app, any notification counts as the answer, and the command is
    sent again if the saber stays silent.
    """
    for attempt in range(1, attempts + 1):
        await send(command)
        try:
            item = await receive(REPLY_TIMEOUT)
        except TimeoutError:
            _LOGGER.debug("No answer to %s (attempt %d)", command, attempt)
            continue
        _LOGGER.debug("Answer to the transfer request: %s", item[:48])
        break
    else:
        raise TransferError(
            f"The saber did not answer the transfer request "
            f"({attempts} attempts, {REPLY_TIMEOUT:.0f} s each)"
        )
    reply = _json_reply(item)
    if reply is not None and reply[0] == p.MSG_ERROR:
        raise TransferError(f"The saber refused the transfer: {reply[1]}")
    if item[:1] == bytes((CAN,)):
        raise TransferError("The saber cancelled the transfer")
    return item


async def send_file(
    send: Send,
    receive: Receive,
    path: str,
    data: bytes,
    *,
    header_name: str | None = None,
    progress: Progress | None = None,
) -> None:
    """Send ``data`` to ``path`` on the saber."""
    total = len(data)
    await _request(send, receive, transfer_command(TO_SABER, path))

    header = header_block(header_name or path, total)
    for attempt in range(RETRIES):
        await send(header)
        if await _await_header_ack(receive):
            break
        _LOGGER.debug("No ACK+C after header (attempt %d)", attempt + 1)
    else:
        raise TransferError("The saber did not acknowledge the file header")

    sent = 0
    seq = 1
    while sent < total:
        chunk = data[sent : sent + BLOCK_PAYLOAD]
        await _send_with_ack(send, receive, data_block(seq, chunk), f"block {seq}")
        sent += len(chunk)
        seq += 1
        if progress is not None:
            progress(sent, total)

    await _finish_send(send, receive)


async def _await_header_ack(receive: Receive) -> bool:
    acked = False
    while True:
        try:
            item = await receive(PACKET_TIMEOUT)
        except TimeoutError:
            return False
        if len(item) > _CONTROL_MAX:
            continue  # a late reply to the command, or noise
        for byte in item:
            if byte == ACK:
                acked = True
            elif byte == CRC_REQUEST and acked:
                return True
            elif byte == NAK:
                return False
            elif byte == CAN:
                raise TransferError("The saber cancelled the transfer")


async def _send_with_ack(send: Send, receive: Receive, packet: bytes, what: str) -> None:
    for attempt in range(RETRIES):
        await send(packet)
        try:
            while True:
                item = await receive(PACKET_TIMEOUT)
                if not item or len(item) > _CONTROL_MAX:
                    continue  # a state message or noise
                code = item[0]
                if code == ACK:
                    return
                if code == CAN:
                    raise TransferError("The saber cancelled the transfer")
                if code == NAK:
                    break
                # 'C' or noise: keep waiting for the verdict on this packet.
        except TimeoutError:
            pass
        _LOGGER.debug("Resending %s (attempt %d)", what, attempt + 2)
    raise TransferError(f"The saber did not acknowledge {what}")


async def _finish_send(send: Send, receive: Receive) -> None:
    for _ in range(RETRIES):
        await send(bytes((EOT,)))
        try:
            item = await receive(PACKET_TIMEOUT)
        except TimeoutError:
            continue
        if item[:1] == bytes((ACK,)):
            return
        if item[:1] == bytes((NAK,)):
            await _send_with_ack(send, receive, bytes((EOT,)), "end of file")
            return
        if item[:1] == bytes((CAN,)):
            raise TransferError("The saber cancelled the transfer")
    raise TransferError("The saber did not confirm the end of the file")


async def receive_file(
    send: Send,
    receive: Receive,
    path: str,
    *,
    progress: Progress | None = None,
    attempts: int = REQUEST_ATTEMPTS,
) -> bytes:
    """Read ``path`` from the saber."""
    await _request(send, receive, transfer_command(FROM_SABER, path), attempts)
    await send(bytes((CRC_REQUEST,)))

    reader = _FrameReader()
    header = await reader.next_frame(receive, SOH)
    _name, size = parse_header_block(header)
    await send(bytes((ACK,)))
    await asyncio.sleep(0.1)  # the app pauses before asking for the data
    await send(bytes((CRC_REQUEST,)))

    out = bytearray()
    last_seq: int | None = None
    eot_seen = False
    while True:
        frame = await reader.next_frame(receive, BLOCK_START, allow_eot=True)
        if frame == bytes((EOT,)):
            if not eot_seen:
                eot_seen = True
                await send(bytes((NAK,)))
                continue
            await send(bytes((ACK,)))
            break
        seq, payload = parse_data_block(frame)
        if seq != last_seq:  # a repeat means our ACK was lost
            out += payload[: max(0, size - len(out))]
            last_seq = seq
            if progress is not None:
                progress(len(out), size)
        await send(bytes((ACK,)))
    if len(out) != size:
        raise TransferError(f"Received {len(out)} of {size} bytes")
    return bytes(out)


class _FrameReader:
    """Reassemble blocks the link may have split across notifications."""

    def __init__(self) -> None:
        self._buf = bytearray()

    async def next_frame(self, receive: Receive, start: int, allow_eot: bool = False) -> bytes:
        length = HEADER_LEN if start == SOH else BLOCK_LEN
        while True:
            if not self._buf:
                try:
                    item = await receive(PACKET_TIMEOUT)
                except TimeoutError as err:
                    raise TransferError("The saber stopped sending") from err
                if _json_reply(item) is not None:
                    continue
                self._buf += item
            if allow_eot and self._buf[0] == EOT:
                del self._buf[0]
                return bytes((EOT,))
            if self._buf[0] == CAN:
                raise TransferError("The saber cancelled the transfer")
            if self._buf[0] != start:
                del self._buf[0]  # stray control byte
                continue
            while len(self._buf) < length:
                try:
                    self._buf += await receive(PACKET_TIMEOUT)
                except TimeoutError as err:
                    raise TransferError("The saber stopped mid-block") from err
            frame = bytes(self._buf[:length])
            del self._buf[:length]
            return frame
