"""BLE client for Xenopixel V3 compatible sabers. Depends on bleak only (no Home Assistant)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator
import contextlib
from dataclasses import dataclass
import logging
from types import ModuleType
from typing import Any
import weakref

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

from . import protocol as p, transfer as t

# bleak-retry-connector ships with Home Assistant; the CLI tool can run without it.
_retry_connector: ModuleType | None
try:
    import bleak_retry_connector as _retry_connector
except ImportError:  # pragma: no cover
    _retry_connector = None

EstablishConnection = Callable[..., Awaitable[BleakClient]]
establish_connection: EstablishConnection | None = (
    _retry_connector.establish_connection if _retry_connector is not None else None
)
BleakClientWithServiceCache: type[BleakClient] = (
    _retry_connector.BleakClientWithServiceCache
    if _retry_connector is not None
    else BleakClient
)

_LOGGER = logging.getLogger(__name__)

HANDSHAKE_TIMEOUT = 5.0
STATE_SETTLE = 0.5
SESSION_ATTEMPTS = 3
RETRY_DELAY = 1.0
REPLY_TIMEOUT = 5.0  # file-system commands (ls, df, rm...)
FONT_NAMES_WINDOW = 1.5  # font names trickle in after `ls "/"`

_QUERIES = frozenset((p.FILE_LS, p.FILE_DF))  # read-only: safe to ask twice
# Replies to file-system commands: never stored as saber state.
_FILE_REPLY_KEYS = frozenset(
    {p.FILE_LS, p.FILE_DF, p.FILE_SETNAME, p.FILE_PLAY, p.FILE_CP, p.FILE_RM, p.FILE_TRANSFER}
)


# One connection attempt at a time per event loop: with several sabers on one
# adapter, simultaneous attempts make BlueZ fail with "InProgress".
_CONNECT_LOCKS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = (
    weakref.WeakKeyDictionary()
)


def _connect_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _CONNECT_LOCKS.get(loop)
    if lock is None:
        lock = _CONNECT_LOCKS[loop] = asyncio.Lock()
    return lock


class XenoSaberError(Exception):
    """Raised when talking to the saber fails."""


class XenoSaberUnreachable(XenoSaberError):
    """The saber could not be reached at all (off, asleep or out of range)."""


class XenoSaberBusy(XenoSaberError):
    """A file transfer is running; other commands must wait for it."""


@dataclass
class TransferStatus:
    """Progress of the running file transfer."""

    direction: str  # transfer.TO_SABER or transfer.FROM_SABER
    path: str
    done: int = 0
    total: int = 0

    @property
    def percent(self) -> int:
        return int(self.done * 100 / self.total) if self.total else 0


@dataclass
class BatchStatus:
    """A multi-file operation (font export, upload, copy): files done so far."""

    total: int
    done: int = 0


class XenoSaber:
    """Connection, handshake, state tracking and commands for one saber."""

    def __init__(
        self,
        ble_device: BLEDevice | None,
        idle_disconnect: float = 0,
        name: str | None = None,
        *,
        address: str | None = None,
    ) -> None:
        # None while the saber sleeps: it is filled in when it advertises.
        self._ble_device = ble_device
        if ble_device is None and address is None:
            raise ValueError("an address is needed without a BLE device")
        self._address = ble_device.address if ble_device is not None else str(address)
        self._name = name  # every saber advertises as "Saber": prefer the entry title
        self.idle_disconnect = idle_disconnect
        self._client: BleakClient | None = None
        self._lock = asyncio.Lock()
        self._assembler = p.MessageAssembler()  # DAE1
        self._data_assembler = p.MessageAssembler()  # 3AB1: fragments must not mix
        self._handshake_event = asyncio.Event()
        self._disconnected_event = asyncio.Event()
        self._listeners: list[Callable[[], None]] = []
        self._idle_handle: asyncio.TimerHandle | None = None
        self._expected_disconnect = False
        self._unavailable_logged = False
        # While set, raw notifications go to the file transfer instead of the parser.
        self._transfer_queue: asyncio.Queue[bytes] | None = None
        self._reply_queues: dict[str, list[asyncio.Queue[Any]]] = {}
        self.transfer: TransferStatus | None = None
        self.batch: BatchStatus | None = None
        # Font names learned from setname/fontname replies; the saber does not
        # always report them when listing.
        self.font_names: dict[int, str] = {}
        self.state: dict[str, Any] = {}
        self.authorized: bool | None = None

    # ------------------------------------------------------------------ props
    @property
    def address(self) -> str:
        return self._address

    @property
    def name(self) -> str:
        advertised = self._ble_device.name if self._ble_device is not None else None
        return self._name or advertised or "Xeno saber"

    @name.setter
    def name(self, value: str) -> None:
        self._name = value

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    @property
    def blade_on(self) -> bool | None:
        value = self.state.get(p.KEY_POWER_ON)
        return bool(value) if value is not None else None

    @property
    def color(self) -> tuple[int, int, int] | None:
        return p.parse_color(self.state.get(p.KEY_COLOR))

    @property
    def battery(self) -> int | None:
        value = self.state.get(p.KEY_BATTERY)
        return int(value) if isinstance(value, (int, float)) else None

    def set_ble_device(self, ble_device: BLEDevice) -> None:
        """Update the BLEDevice (new adapter/proxy path) from advertisements."""
        self._ble_device = ble_device
        self._address = ble_device.address

    # -------------------------------------------------------------- listeners
    def register_callback(self, callback: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(callback)

        def _remove() -> None:
            if callback in self._listeners:
                self._listeners.remove(callback)

        return _remove

    def _notify_listeners(self) -> None:
        for callback in list(self._listeners):
            try:
                callback()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Error in Xeno Saber BLE listener")

    # ------------------------------------------------------------- connection
    async def connect(self) -> None:
        async with self._lock:
            await self._ensure_connected()

    async def _ensure_connected(self) -> None:
        if self.is_connected:
            return
        if self._ble_device is None:
            raise XenoSaberUnreachable(
                "The saber has not been seen since Home Assistant started: wake it up"
            )
        last_err: XenoSaberError | None = None
        for attempt in range(1, SESSION_ATTEMPTS + 1):
            try:
                await self._connect_once()
                return
            except XenoSaberUnreachable:
                # establish_connection already retried; the saber is off or gone.
                raise
            except XenoSaberError as err:
                last_err = err
                _LOGGER.debug(
                    "%s: attempt %d/%d failed: %s",
                    self.address, attempt, SESSION_ATTEMPTS, err,
                )
                if attempt < SESSION_ATTEMPTS:
                    await asyncio.sleep(RETRY_DELAY)
        assert last_err is not None
        raise last_err

    async def _connect_once(self) -> None:
        self._assembler.reset()
        self._data_assembler.reset()
        self._handshake_event.clear()
        self._disconnected_event.clear()
        self._expected_disconnect = False
        _LOGGER.debug("%s: connecting", self.address)
        try:
            async with _connect_lock():
                client = await self._establish()
        except (BleakError, asyncio.TimeoutError) as err:
            raise XenoSaberUnreachable(f"Connection failed: {err}") from err

        self._client = client
        try:
            await self._setup_session(client)
        except Exception:
            await self._disconnect_client()
            raise

    async def _establish(self) -> BleakClient:
        assert self._ble_device is not None  # checked by _ensure_connected
        if establish_connection is not None:
            return await establish_connection(
                BleakClientWithServiceCache,
                self._ble_device,
                self.name,
                disconnected_callback=self._on_disconnect,
                max_attempts=3,
            )
        client = BleakClient(self._ble_device, disconnected_callback=self._on_disconnect)
        await client.connect()
        return client

    async def _wait_handshake(self) -> None:
        """Wait for the handshake reply, failing fast if the link drops."""
        handshake = asyncio.ensure_future(self._handshake_event.wait())
        dropped = asyncio.ensure_future(self._disconnected_event.wait())
        try:
            done, _ = await asyncio.wait(
                {handshake, dropped},
                timeout=HANDSHAKE_TIMEOUT,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            handshake.cancel()
            dropped.cancel()
        if self._handshake_event.is_set():
            return
        if self._disconnected_event.is_set():
            raise XenoSaberError(
                "Saber disconnected during handshake (powered off, asleep or out of range)"
            )
        _LOGGER.debug("%s: no handshake reply, continuing", self.address)

    @staticmethod
    async def _resolve_mtu(client: BleakClient) -> int | None:
        """Ask BlueZ for the negotiated MTU (avoids bleak's default-MTU warning)."""
        backend = getattr(client, "_backend", None)
        acquire = getattr(backend, "_acquire_mtu", None)
        if acquire is not None:
            try:
                await acquire()
            except Exception:  # noqa: BLE001 - best effort, informational only
                return None
        try:
            return client.mtu_size
        except Exception:  # noqa: BLE001
            return None

    async def _setup_session(self, client: BleakClient) -> None:
        """Handshake + authorize, mirroring w5.d / utils.f in the app."""
        services = client.services
        has_handshake = services.get_characteristic(p.HANDSHAKE_CHAR_UUID) is not None
        if services.get_characteristic(p.DATA_CHAR_UUID) is None:
            raise XenoSaberError("Data characteristic 3AB1 not found: not a Xeno V3?")

        try:
            if has_handshake:
                await client.start_notify(p.HANDSHAKE_CHAR_UUID, self._on_notify)
                await self._write(client, p.HANDSHAKE_CHAR_UUID, p.handshake_message())
                await self._wait_handshake()

            await client.start_notify(p.DATA_CHAR_UUID, self._on_data_notify)
            await self._write(client, p.DATA_CHAR_UUID, p.authorize_message())
            raw = await client.read_gatt_char(p.DATA_CHAR_UUID)
        except (BleakError, asyncio.TimeoutError) as err:
            raise XenoSaberError(f"Session setup failed: {err}") from err

        self._assembler.reset()
        self._data_assembler.reset()
        msg = p.parse_message(raw)
        if msg and msg[0] == p.MSG_STATE:
            self._apply_state(msg[1])
        self.authorized = self.state.get(p.AUTHORIZE_KEY) == p.AUTHORIZE_OK
        if not self.authorized:
            # The official app does not block on this either.
            _LOGGER.warning(
                "%s: authorize reply was %r; continuing anyway",
                self.address,
                self.state.get(p.AUTHORIZE_KEY),
            )
        _LOGGER.debug(
            "%s: session ready (mtu=%s)", self.address, await self._resolve_mtu(client)
        )
        await asyncio.sleep(STATE_SETTLE)  # let the initial state dump arrive
        if self._unavailable_logged:
            _LOGGER.info("%s: connection restored", self.address)
            self._unavailable_logged = False
        self._notify_listeners()

    async def disconnect(self) -> None:
        async with self._lock:
            self._cancel_idle_timer()
            await self._disconnect_client()

    async def _disconnect_client(self) -> None:
        client, self._client = self._client, None
        if client is None:
            return
        self._expected_disconnect = True
        try:
            await client.disconnect()
        except BleakError:
            pass

    def _on_disconnect(self, client: BleakClient) -> None:
        # Ignore late callbacks from a client we already replaced (retries).
        if self._client is not None and client is not self._client:
            return
        if not self._expected_disconnect and not self._unavailable_logged:
            # Logged once; the "restored" message below closes the episode.
            _LOGGER.info(
                "%s: connection lost (saber off, asleep or out of range); "
                "it will reconnect when the saber is available again",
                self.address,
            )
            self._unavailable_logged = True
        self._client = None
        self._disconnected_event.set()
        if self._transfer_queue is not None:
            self._transfer_queue.put_nowait(b"")  # wake the transfer so it fails fast
        self._notify_listeners()

    # ----------------------------------------------------------- idle timer
    def _cancel_idle_timer(self) -> None:
        if self._idle_handle is not None:
            self._idle_handle.cancel()
            self._idle_handle = None

    def _schedule_idle_disconnect(self) -> None:
        self._cancel_idle_timer()
        if self.idle_disconnect and self.idle_disconnect > 0:
            loop = asyncio.get_running_loop()
            self._idle_handle = loop.call_later(
                self.idle_disconnect,
                lambda: asyncio.ensure_future(self.disconnect()),
            )

    # ---------------------------------------------------------- notifications
    def _on_notify(self, _char: Any, data: bytearray) -> None:
        """DAE1: handshake and state pushes."""
        if self._transfer_queue is not None:
            raw = bytes(data)
            _LOGGER.debug("%s: transfer, DAE1 notification %s", self.address, raw[:48])
            # The saber might answer the transfer here; never pass on state
            # fragments, which could look like control bytes.
            if p.FILE_TRANSFER.encode() in raw or (raw and set(raw) <= t.CONTROL_BYTES):
                self._transfer_queue.put_nowait(raw)
                return
        for msg_type, payload in self._assembler.feed(data):
            self._handle_message(msg_type, payload)

    def _on_data_notify(self, _char: Any, data: bytearray) -> None:
        """3AB1: commands' replies, and the file transfer while one runs."""
        if self._transfer_queue is not None:
            self._route_transfer_notification(bytes(data))
            return
        for msg_type, payload in self._data_assembler.feed(data):
            self._handle_message(msg_type, payload)

    def _handle_message(self, msg_type: int, payload: dict[str, Any]) -> None:
        if msg_type == p.MSG_STATE:
            if p.HANDSHAKE_KEY in payload:
                self._handshake_event.set()
            self._apply_state(payload)
            self._dispatch_replies(payload)
            self._notify_listeners()
        elif msg_type == p.MSG_ERROR:
            _LOGGER.warning("%s: saber reported error %s", self.address, payload)

    def _route_transfer_notification(self, data: bytes) -> None:
        """3AB1 during a transfer: everything goes to it, as in the app.

        State pushes are applied as well, but still handed over: the app takes
        any notification as the saber's answer to the transfer request.
        """
        assert self._transfer_queue is not None
        _LOGGER.debug("%s: transfer, 3AB1 notification %s", self.address, data[:48])
        if data.startswith(b"["):
            msg = p.parse_message(data)
            if msg is not None and msg[0] == p.MSG_STATE and p.FILE_TRANSFER not in msg[1]:
                self._handle_message(*msg)
        self._transfer_queue.put_nowait(data)

    def _apply_state(self, payload: dict[str, Any]) -> None:
        for key, value in payload.items():
            if key == p.HANDSHAKE_KEY or key in _FILE_REPLY_KEYS:
                continue
            if key == p.KEY_FONT_NAME and not isinstance(value, str):
                continue  # ["/3","name"]: a reply about another font, not the active one
            self.state[key] = value

    def _dispatch_replies(self, payload: dict[str, Any]) -> None:
        for key in (p.FILE_SETNAME, p.FILE_FONTNAME):
            parsed = p.parse_font_name(payload.get(key))
            if parsed is not None:
                self.font_names[parsed[0]] = parsed[1]
        for key, value in payload.items():
            for queue in self._reply_queues.get(key, ()):
                queue.put_nowait(value)

    # --------------------------------------------------------------- commands
    @staticmethod
    async def _write(client: BleakClient, char_uuid: str, data: bytes) -> None:
        char = client.services.get_characteristic(char_uuid)
        response = char is None or "write" in char.properties
        _LOGGER.debug("write %s: %s", char_uuid[4:8], data)
        await client.write_gatt_char(char_uuid, data, response=response)

    async def send(self, key: str, value: Any) -> None:
        """Send ``[2,{key:value}]`` and optimistically update local state."""
        await self.send_raw(p.encode_command(key, value), key, value)

    def _check_not_busy(self) -> None:
        if self.transfer is not None:
            raise XenoSaberBusy(
                f"A file transfer is in progress ({self.transfer.path}, "
                f"{self.transfer.percent}%); try again when it is done"
            )

    async def send_raw(self, data: bytes, key: str | None = None, value: Any = None) -> None:
        self._check_not_busy()
        async with self._lock:
            self._cancel_idle_timer()
            await self._ensure_connected()
            assert self._client is not None
            try:
                await self._write(self._client, p.DATA_CHAR_UUID, data)
            except (BleakError, asyncio.TimeoutError) as err:
                await self._disconnect_client()
                raise XenoSaberError(f"Write failed: {err}") from err
            if key is not None:
                self.state[key] = value
            self._schedule_idle_disconnect()
        self._notify_listeners()

    async def reconnect(self) -> None:
        """Drop the link (if any) and open a fresh session."""
        self._check_not_busy()
        async with self._lock:
            self._cancel_idle_timer()
            await self._disconnect_client()
            await self._ensure_connected()
            self._schedule_idle_disconnect()  # on-demand mode: release it again

    async def keepalive(self) -> bool:
        """Resend the current brightness so the saber does not go to sleep.

        Never reconnects: a saber that is already asleep or out of range is left
        alone. The value sent is the one the saber last reported, so nothing
        visible changes. Returns True if a keepalive was sent.

        Keeping the saber awake also keeps its board and Bluetooth radio
        powered, which drains the battery.
        """
        if self.transfer is not None:
            return False  # the transfer itself keeps the saber awake
        async with self._lock:
            if not self.is_connected:
                return False
            value = self.state.get(p.KEY_BRIGHTNESS)
            if not isinstance(value, int):
                return False
            assert self._client is not None
            try:
                await self._write(
                    self._client, p.DATA_CHAR_UUID, p.encode_command(p.KEY_BRIGHTNESS, value)
                )
            except (BleakError, asyncio.TimeoutError) as err:
                _LOGGER.debug("%s: keepalive failed: %s", self.address, err)
                return False
            return True

    async def set_blade(self, on: bool) -> None:
        await self.send(p.KEY_POWER_ON, on)

    async def set_color(self, red: int, green: int, blue: int) -> None:
        rgb = [max(0, min(255, int(c))) for c in (red, green, blue)]
        await self.send_raw(p.encode_color(*rgb), p.KEY_COLOR, rgb)

    @contextlib.contextmanager
    def batch_progress(self, total: int) -> Iterator[BatchStatus]:
        """Report progress over several files instead of one file at a time."""
        self.batch = BatchStatus(total)
        self._notify_listeners()
        try:
            yield self.batch
        finally:
            self.batch = None
            self._notify_listeners()

    def batch_step(self) -> None:
        if self.batch is not None:
            self.batch.done += 1
            self._notify_listeners()

    @property
    def progress(self) -> int | None:
        """Overall progress in %, or None when nothing is running."""
        if self.batch is not None and self.batch.total:
            current = self.transfer.percent / 100 if self.transfer is not None else 0
            return min(100, int((self.batch.done + current) * 100 / self.batch.total))
        if self.transfer is not None:
            return self.transfer.percent
        return None

    # ----------------------------------------------------------- file system
    async def file_command(
        self, key: str, value: Any, timeout: float | None = None
    ) -> Any:
        """Send a file-system command and return the saber's reply for ``key``."""
        wait = REPLY_TIMEOUT if timeout is None else timeout
        queue: asyncio.Queue[Any] = asyncio.Queue()
        self._reply_queues.setdefault(key, []).append(queue)
        # Queries are sent again once if the reply is lost; changes are not.
        attempts = 2 if key in _QUERIES else 1
        try:
            for attempt in range(1, attempts + 1):
                await self.send_raw(p.encode_command(key, value))
                try:
                    return await asyncio.wait_for(queue.get(), wait)
                except TimeoutError as err:
                    if attempt == attempts:
                        raise XenoSaberError(f"No reply from the saber to {key!r}") from err
                    _LOGGER.debug("%s: no reply to %s, asking again", self.address, key)
            raise AssertionError("unreachable")
        finally:
            self._reply_queues[key].remove(queue)

    async def list_fonts(self) -> dict[int, str | None]:
        """Font folders on the saber's card, with the names it reports."""
        names: asyncio.Queue[Any] = asyncio.Queue()
        self._reply_queues.setdefault(p.FILE_FONTNAME, []).append(names)
        try:
            folders = p.parse_font_folders(await self.file_command(p.FILE_LS, "/"))
            fonts: dict[int, str | None] = dict.fromkeys(folders)
            loop = asyncio.get_running_loop()
            deadline = loop.time() + FONT_NAMES_WINDOW
            while any(v is None for v in fonts.values()):
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    parsed = p.parse_font_name(await asyncio.wait_for(names.get(), remaining))
                except TimeoutError:
                    break
                if parsed is not None and parsed[0] in fonts:
                    fonts[parsed[0]] = parsed[1]
            for number in fonts:
                if fonts[number] is None:
                    fonts[number] = self.font_names.get(number)
            # The active font's name is in the state; its index is 0-based.
            active, name = self.state.get(p.KEY_FONT), self.state.get(p.KEY_FONT_NAME)
            if isinstance(active, int) and isinstance(name, str) and 0 <= active < len(folders):
                fonts[folders[active]] = fonts[folders[active]] or name
            return fonts
        finally:
            self._reply_queues[p.FILE_FONTNAME].remove(names)

    async def list_sounds(self, font: int) -> dict[str, tuple[int, int]]:
        """Index range of each sound type in font folder ``font``."""
        reply = await self.file_command(p.FILE_LS, f"/{font}")
        if reply is None:
            return {}
        try:
            return p.parse_sound_ranges(reply)
        except ValueError as err:
            raise XenoSaberError(str(err)) from err

    async def storage(self) -> tuple[int, int]:
        """(used, total) storage in MiB, as the app shows it."""
        reply = await self.file_command(p.FILE_DF, "")
        if not isinstance(reply, list) or len(reply) < 2:
            raise XenoSaberError(f"Unexpected storage reply: {reply!r}")
        return int(reply[0]), int(reply[1])

    # --------------------------------------------------------- file transfer
    async def send_file(
        self, path: str, data: bytes, *, header_name: str | None = None
    ) -> None:
        """Write ``data`` to ``path`` on the saber (sound file, config, firmware)."""

        async def run(send: t.Send, receive: t.Receive, status: TransferStatus) -> None:
            await t.send_file(
                send, receive, path, data,
                header_name=header_name, progress=self._progress_updater(status),
            )

        await self._run_transfer(TransferStatus(t.TO_SABER, path, 0, len(data)), run)

    async def receive_file(self, path: str, *, attempts: int = t.REQUEST_ATTEMPTS) -> bytes:
        """Read ``path`` from the saber (``attempts=1`` to probe optional files)."""
        result = bytearray()

        async def run(send: t.Send, receive: t.Receive, status: TransferStatus) -> None:
            result.extend(
                await t.receive_file(
                    send, receive, path,
                    progress=self._progress_updater(status), attempts=attempts,
                )
            )

        await self._run_transfer(TransferStatus(t.FROM_SABER, path), run)
        return bytes(result)

    def _progress_updater(self, status: TransferStatus) -> Callable[[int, int], None]:
        def _update(done: int, total: int) -> None:
            before = status.percent
            status.done, status.total = done, total
            if status.percent != before:
                self._notify_listeners()

        return _update

    async def _run_transfer(
        self,
        status: TransferStatus,
        run: Callable[[t.Send, t.Receive, TransferStatus], Awaitable[None]],
    ) -> None:
        self._check_not_busy()
        async with self._lock:
            self._cancel_idle_timer()
            await self._ensure_connected()
            client = self._client
            assert client is not None
            mtu = await self._resolve_mtu(client)
            if mtu is not None and mtu < t.MIN_MTU:
                self._schedule_idle_disconnect()
                raise XenoSaberError(
                    f"File transfer needs a Bluetooth MTU of at least {t.MIN_MTU} "
                    f"(this link has {mtu}); use a closer adapter or an ESPHome "
                    "proxy that negotiates a larger MTU"
                )
            queue: asyncio.Queue[bytes] = asyncio.Queue()

            async def send(packet: bytes) -> None:
                if not client.is_connected:
                    raise t.TransferError("The saber disconnected")
                await self._write(client, p.DATA_CHAR_UUID, packet)

            async def receive(timeout: float) -> bytes:
                item = await asyncio.wait_for(queue.get(), timeout)
                if not item and not client.is_connected:
                    raise t.TransferError("The saber disconnected")
                return item

            self._transfer_queue = queue
            self.transfer = status
            self._notify_listeners()
            _LOGGER.debug("%s: %s %s started", self.address, status.direction, status.path)
            try:
                await run(send, receive, status)
            except (t.TransferError, BleakError, TimeoutError) as err:
                raise XenoSaberError(f"File transfer failed ({status.path}): {err}") from err
            finally:
                self._transfer_queue = None
                self._assembler.reset()
                self._data_assembler.reset()
                self.transfer = None
                if self.is_connected:
                    self._schedule_idle_disconnect()
                self._notify_listeners()
            _LOGGER.debug("%s: %s %s done", self.address, status.direction, status.path)
