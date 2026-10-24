"""WLED notifier parsing and the shared UDP listener (real sockets on localhost)."""

import asyncio
import importlib
from pathlib import Path
import socket
import sys
import types

_PKG_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "xeno_saber_ble"
_pkg = types.ModuleType("_xsb_standalone")
_pkg.__path__ = [str(_PKG_DIR)]
sys.modules.setdefault("_xsb_standalone", _pkg)
w = importlib.import_module("_xsb_standalone.wled_udp")


def packet(bri, r, g, b, proto=0):
    # Notifier layout: [protocol, callMode, brightness, R, G, B, ...] (real packets are longer).
    return bytes([proto, 1, bri, r, g, b] + [0] * 18)


def test_parse_notifier():
    assert w.parse_notifier(packet(255, 10, 20, 30)) == w.WledState(True, 255, (10, 20, 30))
    assert w.parse_notifier(packet(0, 10, 20, 30)) == w.WledState(False, 0, (10, 20, 30))


def test_parse_ignores_other_protocols_and_short_packets():
    assert w.parse_notifier(packet(255, 1, 2, 3, proto=2)) is None  # DRGB realtime
    assert w.parse_notifier(b"\x00\x01\xff") is None
    assert w.parse_notifier(b"") is None


def _send(port, data):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.sendto(data, ("127.0.0.1", port))


def test_hub_shares_one_socket_and_closes_with_last_listener():
    async def go():
        hub = w.WledUdpHub(port=0, host="127.0.0.1")
        got_a, got_b = [], []
        remove_a = await hub.add_listener(got_a.append)
        port = hub.port
        remove_b = await hub.add_listener(got_b.append)
        assert hub.port == port  # same socket
        _send(port, packet(128, 255, 0, 0))
        _send(port, b"garbage")
        await asyncio.sleep(0.05)
        assert got_a == got_b == [w.WledState(True, 128, (255, 0, 0))]
        remove_a()
        assert hub._transport is not None
        remove_b()
        assert hub._transport is None

    asyncio.run(go())


def test_hub_source_filter():
    async def go():
        hub = w.WledUdpHub(port=0, host="127.0.0.1")
        allowed, blocked = [], []
        remove_1 = await hub.add_listener(allowed.append, "127.0.0.1")
        remove_2 = await hub.add_listener(blocked.append, "192.168.1.50")
        _send(hub.port, packet(255, 0, 0, 255))
        await asyncio.sleep(0.05)
        assert len(allowed) == 1 and blocked == []
        remove_1()
        remove_2()

    asyncio.run(go())


def test_bind_failure_raises_and_leaves_no_socket():
    import pytest

    async def go():
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as taken:
            taken.bind(("127.0.0.1", 0))  # no SO_REUSEADDR: the port is exclusive
            hub = w.WledUdpHub(port=taken.getsockname()[1], host="127.0.0.1")
            with pytest.raises(OSError):
                await hub.add_listener(lambda s: None)
            assert hub._transport is None

    asyncio.run(go())


def test_a_failing_listener_does_not_stop_the_others(caplog):
    hub = w.WledUdpHub(port=0, host="127.0.0.1")
    got = []

    def broken(state):
        raise RuntimeError("bug")

    hub._listeners = [(broken, None), (got.append, None)]
    hub.dispatch(packet(255, 1, 2, 3), "127.0.0.1")
    assert len(got) == 1
    assert "Error in WLED sync listener" in caplog.text
