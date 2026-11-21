"""The filtering D-Bus proxy, run for real against a session bus that has systemd on it.

The escape this closes: over the session bus, a contained process asked the user's systemd
to run a command outside the sandbox. Through the proxy the server may talk to the key store
and to nothing else. Runs only where there is a session bus with systemd, ``xdg-dbus-proxy``
and the D-Bus library the key store client uses (``jeepney``); elsewhere it skips.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from entrabot.sandbox import dbusproxy

jeepney = pytest.importorskip("jeepney", reason="the key store's D-Bus library (Linux)")
PROXY = shutil.which(dbusproxy.EXECUTABLE)
BUS = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
pytestmark = pytest.mark.skipif(
    not (sys.platform.startswith("linux") and PROXY and BUS),
    reason="needs Linux, a session bus and xdg-dbus-proxy",
)

SYSTEMD = "org.freedesktop.systemd1"


def _call(address: str, destination: str, path: str, interface: str, method: str):
    from jeepney import DBusAddress, new_method_call
    from jeepney.io.blocking import open_dbus_connection

    with open_dbus_connection(bus=address) as connection:
        target = DBusAddress(path, bus_name=destination, interface=interface)
        return connection.send_and_get_reply(new_method_call(target, method), timeout=10)


def _names(address: str) -> list[str]:
    reply = _call(
        address,
        "org.freedesktop.DBus",
        "/org/freedesktop/DBus",
        "org.freedesktop.DBus",
        "ListNames",
    )
    return list(reply.body[0])


@pytest.fixture
def proxied() -> Iterator[str]:
    if SYSTEMD not in _names(BUS):
        pytest.skip("this session bus has no systemd on it")
    folder = Path(tempfile.mkdtemp(prefix="entrabot-dbus-test-"))
    proxy = dbusproxy.DbusProxy()
    proxy.start(str(PROXY), BUS, folder / "bus.sock")
    try:
        yield f"unix:path={folder / 'bus.sock'}"
    finally:
        proxy.stop()
        shutil.rmtree(folder, ignore_errors=True)


def test_systemd_answers_on_the_real_bus_and_not_through_the_proxy(proxied: str) -> None:
    from jeepney import MessageType

    manager = ("/org/freedesktop/systemd1", "org.freedesktop.systemd1.Manager", "ListUnits")
    direct = _call(BUS, SYSTEMD, *manager)
    assert direct.header.message_type is MessageType.method_return  # the escape's first step
    through = _call(proxied, SYSTEMD, *manager)
    assert through.header.message_type is MessageType.error


def test_the_rest_of_the_bus_is_invisible_through_the_proxy(proxied: str) -> None:
    visible = [name for name in _names(proxied) if not name.startswith(":")]
    assert SYSTEMD in _names(BUS)
    assert SYSTEMD not in visible
    assert set(visible) <= {"org.freedesktop.DBus", *dbusproxy.TALK}


def test_the_proxy_stops_with_its_owner(proxied: str) -> None:
    # Covered by the fixture's stop(); this checks a second proxy leaves nothing behind.
    folder = Path(tempfile.mkdtemp(prefix="entrabot-dbus-test-"))
    proxy = dbusproxy.DbusProxy()
    proxy.start(str(PROXY), BUS, folder / "bus.sock")
    process = proxy._process
    assert process is not None and process.poll() is None
    proxy.stop()
    assert process.poll() is not None
    shutil.rmtree(folder, ignore_errors=True)
