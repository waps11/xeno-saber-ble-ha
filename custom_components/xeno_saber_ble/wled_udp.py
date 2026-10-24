"""Receive WLED UDP sync ("notifier") packets. No Home Assistant imports.

WLED broadcasts a small packet on UDP port 21324 whenever its state changes
(Settings > Sync Interfaces > "Send notifications on change"). Byte 0 is the
protocol (0 = notifier), byte 2 the master brightness (0-255) and bytes 3-5
the primary colour. Other values of byte 0 on the same port are WLED's
real-time streaming protocols and are ignored here.

The socket is shared by every saber and only open while at least one saber
has colour sync on with the UDP source enabled. The packets carry no
authentication, so any host on the LAN can send them; a source address
filter can restrict which device is obeyed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import logging
import socket
from typing import override

_LOGGER = logging.getLogger(__name__)

WLED_UDP_PORT = 21324
_PROTOCOL_NOTIFIER = 0
_MIN_LENGTH = 6


@dataclass(frozen=True)
class WledState:
    """What a notifier packet says: on/off, brightness 0-255, primary colour."""

    on: bool
    brightness: int
    rgb: tuple[int, int, int]


def parse_notifier(data: bytes) -> WledState | None:
    """Decode a WLED notifier packet, or return None for anything else."""
    if len(data) < _MIN_LENGTH or data[0] != _PROTOCOL_NOTIFIER:
        return None
    brightness = data[2]
    return WledState(brightness > 0, brightness, (data[3], data[4], data[5]))


Listener = Callable[[WledState], None]


class _Protocol(asyncio.DatagramProtocol):
    def __init__(self, hub: WledUdpHub) -> None:
        self._hub = hub

    @override
    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self._hub.dispatch(data, addr[0])

    @override
    def error_received(self, exc: Exception) -> None:  # pragma: no cover
        _LOGGER.debug("WLED UDP socket error: %s", exc)


class WledUdpHub:
    """One UDP socket shared by all sabers; open only while someone listens."""

    def __init__(self, port: int = WLED_UDP_PORT, host: str = "0.0.0.0") -> None:
        self._port = port
        self._host = host
        self._transport: asyncio.DatagramTransport | None = None
        self._listeners: list[tuple[Listener, str | None]] = []
        self._start_lock = asyncio.Lock()

    @property
    def port(self) -> int:
        """The bound port (useful when created with port 0 in tests)."""
        if self._transport is None:
            return self._port
        return int(self._transport.get_extra_info("sockname")[1])

    async def add_listener(
        self, listener: Listener, source: str | None = None
    ) -> Callable[[], None]:
        """Register a listener, opening the socket if needed.

        ``source`` limits the listener to packets from that IP address.
        Raises OSError if the port cannot be bound (e.g. already in use).
        """
        async with self._start_lock:
            if self._transport is None:
                await self._open()
            entry = (listener, source or None)
            self._listeners.append(entry)

        def _remove() -> None:
            if entry in self._listeners:
                self._listeners.remove(entry)
            if not self._listeners:
                self.close()

        return _remove

    async def _open(self) -> None:
        loop = asyncio.get_running_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self._host, self._port))
            sock.setblocking(False)
            transport, _ = await loop.create_datagram_endpoint(
                lambda: _Protocol(self), sock=sock
            )
        except OSError:
            sock.close()
            raise
        self._transport = transport
        _LOGGER.debug("Listening for WLED sync on UDP %s", self.port)

    def close(self) -> None:
        transport, self._transport = self._transport, None
        if transport is not None:
            transport.close()
            _LOGGER.debug("Stopped listening for WLED sync")

    def dispatch(self, data: bytes, sender: str) -> None:
        state = parse_notifier(data)
        if state is None:
            return
        for listener, source in list(self._listeners):
            if source is not None and source != sender:
                continue
            try:
                listener(state)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Error in WLED sync listener")
