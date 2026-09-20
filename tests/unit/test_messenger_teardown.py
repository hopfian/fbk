"""Messenger listen teardown suppression (just-landed fix, wave 2).

listen()/listen_dgw() close their realtime client in a finally block; a
close() that raises must never mask the loop's collected result or its
original exception. Both loops construct their client through the
module-level ``MQTTClient`` / ``LightspeedClient`` names in
surfaces.messenger — the sanctioned monkeypatch seam (same pattern the
feed-command tests use for ``new_session``).

Unit (offline): stub clients record calls, a fake monotonic clock runs
the deadline loop deterministically; no network.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fakes import StubSession

import surfaces.messenger as messenger_module
from surfaces.messenger import MessengerService


class FakeMonoClock:
    """Stands in for the ``time`` module name inside surfaces.messenger:
    a frozen monotonic clock the stubs advance to end deadline loops."""

    def __init__(self):
        self.now = 100.0

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return self.now


class StubFrame:
    kind = "PUBLISH"
    topic = "/t_ms"
    payload = b"\x01\x02\x03"

    def thrift_summary(self):
        return {"field": 1}


class StubMQTTClient:
    """Records the session lifecycle; fails on demand per test."""

    def __init__(self, *, frames=(), read_error=None, connect_error=None,
                 close_error=None, clock=None):
        self.frames = list(frames)
        self.read_error = read_error
        self.connect_error = connect_error
        self.close_error = close_error
        self.clock = clock
        self.subscribed: list[tuple[int, str]] = []
        self.close_calls = 0

    def connect(self, user_id):
        if self.connect_error is not None:
            raise self.connect_error

    def subscribe(self, topic, *, packet_id=1):
        self.subscribed.append((packet_id, topic))

    def read(self, timeout=10.0):
        if self.read_error is not None:
            raise self.read_error
        if self.frames:
            return self.frames.pop(0)
        if self.clock is not None:
            self.clock.now += 10_000.0  # push past any deadline, end loop
        return None

    def close(self):
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


class StubDGWResponse:
    request_id = 7
    payload_type = 3

    def __init__(self):
        self.payload = {"sync": "ok"}


class StubLightspeedClient:
    def __init__(self, *, responses=(), read_error=None, connect_error=None,
                 close_error=None, clock=None):
        self.responses = list(responses)
        self.read_error = read_error
        self.connect_error = connect_error
        self.close_error = close_error
        self.clock = clock
        self.subscribed_threads = None
        self.close_calls = 0

    def connect(self, user_id, *, device_id=None):
        if self.connect_error is not None:
            raise self.connect_error

    def subscribe_threads(self, device_id, targets, *, request_id=1):
        self.subscribed_threads = list(targets)

    def read_response(self, timeout=10.0):
        if self.read_error is not None:
            raise self.read_error
        if self.responses:
            return self.responses.pop(0)
        if self.clock is not None:
            self.clock.now += 10_000.0
        return None

    def close(self):
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


def install(monkeypatch, *, mqtt=None, lightspeed=None):
    """Swap in the stub clients AND wire the shared fake clock: the module
    name ``time`` points at it, and the stubs advance it to end deadline
    loops once their frames run dry."""
    clock = FakeMonoClock()
    monkeypatch.setattr(messenger_module, "time", clock)
    if mqtt is not None:
        mqtt.clock = clock
        monkeypatch.setattr(messenger_module, "MQTTClient",
                            lambda cookies, **kw: mqtt)
    if lightspeed is not None:
        lightspeed.clock = clock
        monkeypatch.setattr(messenger_module, "LightspeedClient",
                            lambda cookies, channel: lightspeed)
    return clock


class TestListenTeardown:
    """listen(): a close() that raises never masks result or exception."""

    def test_close_failure_does_not_mask_the_collected_result(self, monkeypatch):
        mqtt = StubMQTTClient(frames=[StubFrame()],
                              close_error=RuntimeError("teardown boom"))
        install(monkeypatch, mqtt=mqtt)
        result = MessengerService(StubSession()).listen(seconds=5.0)
        assert [entry["topic"] for entry in result] == ["/t_ms"]
        assert result[0]["kind"] == "PUBLISH"
        assert result[0]["size"] == 3
        assert result[0]["summary"] == {"field": 1}
        assert mqtt.close_calls == 1  # teardown ran, its error swallowed

    def test_close_failure_does_not_mask_a_loop_error(self, monkeypatch):
        mqtt = StubMQTTClient(read_error=ValueError("read blew up"),
                              close_error=RuntimeError("teardown boom"))
        install(monkeypatch, mqtt=mqtt)
        with pytest.raises(ValueError, match="read blew up"):
            MessengerService(StubSession()).listen(seconds=5.0)
        assert mqtt.close_calls == 1

    def test_connect_failure_propagates_despite_close_error(self, monkeypatch):
        mqtt = StubMQTTClient(
            connect_error=ConnectionError("handshake refused"),
            close_error=RuntimeError("teardown boom"))
        install(monkeypatch, mqtt=mqtt)
        with pytest.raises(ConnectionError, match="handshake refused"):
            MessengerService(StubSession()).listen(seconds=5.0)
        assert mqtt.close_calls == 1  # the socket was still torn down

    def test_default_topic_set_is_subscribed(self, monkeypatch):
        """The live-observed web-client pair, with packet ids 1..n."""
        mqtt = StubMQTTClient()
        install(monkeypatch, mqtt=mqtt)
        assert MessengerService(StubSession()).listen(seconds=0.0) == []
        assert mqtt.subscribed == [(1, "/t_ms"), (2, "/t_rtc_multi")]


class TestListenDgwTeardown:
    """listen_dgw(): the same suppression contract on the DGW socket."""

    def test_close_failure_does_not_mask_the_collected_result(self, monkeypatch):
        dgw = StubLightspeedClient(responses=[StubDGWResponse()],
                                   close_error=RuntimeError("teardown boom"))
        install(monkeypatch, lightspeed=dgw)
        result = MessengerService(StubSession()).listen_dgw(seconds=5.0)
        assert result == [{"request_id": 7, "payload_type": 3,
                           "payload": '{"sync":"ok"}'}]
        assert dgw.close_calls == 1

    def test_close_failure_does_not_mask_a_loop_error(self, monkeypatch):
        dgw = StubLightspeedClient(read_error=ValueError("dgw read blew up"),
                                   close_error=RuntimeError("teardown boom"))
        install(monkeypatch, lightspeed=dgw)
        with pytest.raises(ValueError, match="dgw read blew up"):
            MessengerService(StubSession()).listen_dgw(seconds=5.0)
        assert dgw.close_calls == 1

    def test_connect_failure_propagates_despite_close_error(self, monkeypatch):
        dgw = StubLightspeedClient(
            connect_error=ConnectionError("dgw handshake refused"),
            close_error=RuntimeError("teardown boom"))
        install(monkeypatch, lightspeed=dgw)
        with pytest.raises(ConnectionError, match="dgw handshake refused"):
            MessengerService(StubSession()).listen_dgw(seconds=5.0)
        assert dgw.close_calls == 1
