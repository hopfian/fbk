"""MQTT keepalive discipline: PINGREQ cadence (just-landed fix, wave 2).

The cadence runs on SEND time, not idle time: MQTT requires the client
to emit a control packet inside the negotiated 15s window even while
deltas stream in continuously — receiving traffic never satisfies the
keepalive. read() therefore fires a PINGREQ once PING_CADENCE_S (10s)
elapses since the last ping; ping() stamps the clock; connect() starts
it.

Unit (offline): a stub _ws records sends, a frozen fake clock (swapped
in for the module's ``time``) makes the cadence arithmetic exact, and
coherent_ws_connect is stubbed for the connect() test. No network, no
real socket.
"""
from __future__ import annotations

import pytest

import realtime.mqtt.client as mqtt_client
from realtime.mqtt import frames
from realtime.mqtt.client import MQTTClient, MQTTClientError

T0 = 1_000_000.0
PINGREQ = frames.encode_pingreq()  # C0 00
CONNACK_OK = bytes([0x20, 0x02, 0x00, 0x00])


class FakeTime:
    """A frozen epoch clock — advances only when the test says so."""

    def __init__(self, start: float = T0):
        self.now = start

    def time(self) -> float:
        return self.now

    def advance(self, s: float) -> None:
        self.now += s


class StubWS:
    """The WSConnection seam: records sends, serves queued recv frames."""

    def __init__(self, recv_queue=None):
        self.sent: list[bytes] = []
        self.queue = list(recv_queue or [])
        self.closed = 0

    def send(self, data: bytes) -> None:
        self.sent.append(data)

    def recv(self, timeout: float):
        if self.queue:
            return self.queue.pop(0)
        raise TimeoutError("idle")

    def close(self) -> None:
        self.closed += 1


def ping_count(ws: StubWS) -> int:
    return ws.sent.count(PINGREQ)


def connected_client(ws: StubWS, last_ping: float = T0) -> MQTTClient:
    """A client wired to the stub socket, mid-session, cadence armed."""
    client = MQTTClient({"c_user": "1", "xs": "x"})
    client._ws = ws
    client._connected = True
    client._last_ping = last_ping
    return client


class TestSendTimeCadence:
    """Pins read()'s send-time ping: fires on the 10s boundary even under
    continuous inbound traffic, and only then."""

    def test_no_ping_inside_the_cadence_window(self, monkeypatch):
        clock = FakeTime()
        monkeypatch.setattr(mqtt_client, "time", clock)
        ws = StubWS(recv_queue=[b"\xd0\x00"] * 9)
        client = connected_client(ws)
        for _ in range(9):  # one PINGRESP per second, all inside 10s
            clock.advance(1.0)
            client.read()
        assert ping_count(ws) == 0

    def test_ping_fires_exactly_at_the_cadence_boundary(self, monkeypatch):
        clock = FakeTime()
        monkeypatch.setattr(mqtt_client, "time", clock)
        ws = StubWS(recv_queue=[b"\xd0\x00"] * 20)
        client = connected_client(ws)
        for _ in range(10):
            clock.advance(1.0)
            client.read()
        assert ping_count(ws) == 1
        assert client._last_ping == T0 + 10.0  # ping() stamped the clock

    def test_traffic_does_not_reset_the_cadence(self, monkeypatch):
        """The wave-2 pin: inbound frames alone never satisfy keepalive —
        pings still fire despite zero idle reads."""
        clock = FakeTime()
        monkeypatch.setattr(mqtt_client, "time", clock)
        ws = StubWS(recv_queue=[b"\xd0\x00"] * 100)
        client = connected_client(ws)
        for _ in range(35):  # 35s of continuous traffic, no idle tick
            clock.advance(1.0)
            client.read()
        # pings at +10, +20, +30: exactly three, none missed
        assert ping_count(ws) == 3

    def test_one_ping_per_read_even_after_a_long_gap(self, monkeypatch):
        """25s of elapsed time fires ONE ping per read call (the stamp
        resets the clock), never a burst of catch-up pings."""
        clock = FakeTime()
        monkeypatch.setattr(mqtt_client, "time", clock)
        ws = StubWS(recv_queue=[b"\xd0\x00"])
        client = connected_client(ws)
        clock.advance(25.0)
        client.read()
        assert ping_count(ws) == 1
        assert client._last_ping == T0 + 25.0

    def test_idle_read_timeout_also_pings(self, monkeypatch):
        """The pre-existing idle path stays: a recv TimeoutError -> one
        PINGREQ and a None return (the keepalive loop's idle tick)."""
        clock = FakeTime()
        monkeypatch.setattr(mqtt_client, "time", clock)
        ws = StubWS(recv_queue=[])  # recv raises TimeoutError immediately
        client = connected_client(ws)
        clock.advance(0.5)
        assert client.read() is None
        assert ping_count(ws) == 1

    def test_ping_is_a_no_op_when_disconnected(self, monkeypatch):
        clock = FakeTime()
        monkeypatch.setattr(mqtt_client, "time", clock)
        client = MQTTClient({"c_user": "1"})
        client.ping()  # no socket, not connected: must not raise or send
        assert client._last_ping == 0.0


class TestConnectInitializesCadence:
    """Pins connect()'s clock seeding: the handshake counts as client-
    sent traffic, so the first PINGREQ lands ~PING_CADENCE_S after."""

    def test_connect_seeds_the_cadence_clock(self, monkeypatch):
        clock = FakeTime()
        monkeypatch.setattr(mqtt_client, "time", clock)
        ws = StubWS(recv_queue=[b"\x0a", CONNACK_OK])
        monkeypatch.setattr(mqtt_client, "coherent_ws_connect",
                            lambda *a, **k: ws)
        client = MQTTClient({"c_user": "1", "xs": "x"})
        client.connect("12345678901234")
        assert client._connected is True
        assert client._last_ping == T0  # the clock starts at the handshake
        assert client._last_activity == T0
        # the server-first byte was consumed and the CONNECT was sent
        assert ws.sent and ws.sent[0][0] == 0x10  # MQIsdp CONNECT opcode

    def test_first_ping_lands_one_cadence_after_connect(self, monkeypatch):
        clock = FakeTime()
        monkeypatch.setattr(mqtt_client, "time", clock)
        ws = StubWS(recv_queue=[b"\x0a", CONNACK_OK])
        monkeypatch.setattr(mqtt_client, "coherent_ws_connect",
                            lambda *a, **k: ws)
        client = MQTTClient({"c_user": "1", "xs": "x"})
        client.connect("12345678901234")
        ws.queue = [b"\xd0\x00", b"\xd0\x00"]  # continuous inbound traffic

        clock.advance(9.9)
        client.read()
        assert ping_count(ws) == 0  # 0.1s inside the window
        clock.advance(0.1)
        client.read()
        assert ping_count(ws) == 1  # exactly at the boundary

    def test_rejected_connack_closes_the_socket(self, monkeypatch):
        clock = FakeTime()
        monkeypatch.setattr(mqtt_client, "time", clock)
        ws = StubWS(recv_queue=[b"\x0a", bytes([0x20, 0x02, 0x00, 0x05])])
        monkeypatch.setattr(mqtt_client, "coherent_ws_connect",
                            lambda *a, **k: ws)
        client = MQTTClient({"c_user": "1", "xs": "x"})
        with pytest.raises(MQTTClientError, match="CONNACK rejected"):
            client.connect("12345678901234")
        assert client._connected is False
        assert ws.closed == 1  # a refused handshake leaves no live socket
