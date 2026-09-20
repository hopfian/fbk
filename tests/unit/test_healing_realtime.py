"""Realtime listen self-healing reconnects (healing kind realtime-reconnect).

Offline unit tests for the bounded reconnect loop in
``surfaces.messenger.MessengerService._listen_healed``, driven through the
same monkeypatch seams the messenger/realtime suites use: the module-level
``MQTTClient`` / ``LightspeedClient`` names in surfaces.messenger plus a
fake module-``time`` (frozen monotonic clock + recorded sleeps) — no
network, no real sockets, no governor state.

Covers: one drop -> one paced re-dial with exactly one redacted heal row
and preserved frames (MQTT and DGW); persistent failure -> the ORIGINAL
typed error after limit+1 pinned dials and the final "reconnect budget
exhausted" event; ``FBK_HEAL=off`` and a zero/unwired limit -> the
pre-healing single dial with zero rows; normal deadline expiry and
``KeyboardInterrupt`` are not failures (no reconnect, no rows); the
``FBK_HEAL_REALTIME_RECONNECTS`` override (default 3, ceiling clamp 10)
and the pacing discipline (governor gate when reachable, fixed backoff
when not).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fakes import StubSession

import surfaces.messenger as messenger_module
from healing import KIND_REALTIME_RECONNECT, HealingLog, realtime_reconnect_limit
from surfaces.messenger import (
    DGW_DEFAULT_THREAD_ID,
    REALTIME_RECONNECT_BACKOFF_S,
    MessengerService,
)


@pytest.fixture(autouse=True)
def heal_env(monkeypatch):
    """Pin the default healing configuration for every test."""
    for name in ("FBK_HEAL", "FBK_HEAL_REALTIME_RECONNECTS"):
        monkeypatch.delenv(name, raising=False)


class FakeClock:
    """Module-``time`` stand-in: frozen monotonic clock + sleep recorder."""

    def __init__(self, start: float = 1000.0):
        self.now = start
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def advance(self, seconds: float) -> None:
        self.now += seconds


class StubFrame:
    """A received MQTT PUBLISH frame (same shape the teardown stubs use)."""

    kind = "PUBLISH"
    topic = "/t_ms"
    payload = b"\x01\x02\x03"

    def thrift_summary(self):
        return {"field": 1}


class StubResponse:
    """A typed DGW response (rid echo + catalog payload type)."""

    request_id = 7
    payload_type = "state_sync_result"

    def __init__(self):
        self.payload = {"sync": "ok"}


class StubMQTT:
    """One dial's scripted MQTT behavior; records every seam the driver uses."""

    def __init__(self, *, connect_error=None, read_error=None, frames=(),
                 drop=False, advance_before_drop=0.0, clock=None,
                 close_error=None):
        self.connect_error = connect_error
        self.read_error = read_error
        self.frames = list(frames)
        self.drop = drop
        self.advance_before_drop = advance_before_drop
        self.clock = clock
        self.close_error = close_error
        self.subscribed: list[tuple[int, str]] = []
        self.read_timeouts: list[float] = []
        self.close_calls = 0

    def connect(self, user_id):
        if self.connect_error is not None:
            raise self.connect_error

    def subscribe(self, topic, *, packet_id=1):
        self.subscribed.append((packet_id, topic))

    def read(self, timeout=10.0):
        self.read_timeouts.append(timeout)
        if self.read_error is not None:
            raise self.read_error
        if self.frames:
            return self.frames.pop(0)
        if self.drop:
            if self.advance_before_drop:
                self.clock.now += self.advance_before_drop
            raise ConnectionError("ws reader terminated: socket gone")
        self.clock.now += 10_000.0  # idle tick: push past any deadline
        return None

    def close(self):
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


class StubDGW:
    """One dial's scripted DGW behavior; records every seam the driver uses."""

    def __init__(self, *, connect_error=None, read_error=None, responses=(),
                 drop=False, clock=None, close_error=None):
        self.connect_error = connect_error
        self.read_error = read_error
        self.responses = list(responses)
        self.drop = drop
        self.clock = clock
        self.close_error = close_error
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
        if self.drop:
            raise ConnectionError("ws reader terminated: dgw socket gone")
        self.clock.now += 10_000.0
        return None

    def close(self):
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


class RecordingGovernor:
    """Stands in for the session transport's governor; records gate ticks."""

    def __init__(self):
        self.gated: list[bool] = []

    def before_request(self, *, is_mutation: bool = False,
                       quiet: bool = False) -> float:
        self.gated.append(is_mutation)
        return 0.0


def mqtt_seam(monkeypatch, clock: FakeClock,
              scripts: list[StubMQTT]) -> list[StubMQTT]:
    """Swap in the scripted clients + fake clock; return the dials made.

    One factory call == one dial: an extra dial beyond the scripted set
    fails the test loudly instead of silently re-running a script.
    """
    made: list[StubMQTT] = []

    def factory(cookies, **kw):
        if len(made) >= len(scripts):
            raise AssertionError(
                f"unexpected dial {len(made) + 1} (scripted: {len(scripts)})")
        stub = scripts[len(made)]
        made.append(stub)
        return stub

    monkeypatch.setattr(messenger_module, "time", clock)
    monkeypatch.setattr(messenger_module, "MQTTClient", factory)
    return made


def dgw_seam(monkeypatch, clock: FakeClock,
             scripts: list[StubDGW]) -> list[StubDGW]:
    """The DGW twin of :func:`mqtt_seam` (for listen_dgw)."""
    made: list[StubDGW] = []

    def factory(cookies, channel):
        if len(made) >= len(scripts):
            raise AssertionError(
                f"unexpected dial {len(made) + 1} (scripted: {len(scripts)})")
        stub = scripts[len(made)]
        made.append(stub)
        return stub

    monkeypatch.setattr(messenger_module, "time", clock)
    monkeypatch.setattr(messenger_module, "LightspeedClient", factory)
    return made


def heal_log(tmp_path: Path) -> tuple[HealingLog, Path]:
    """A HealingLog over a tmp state dir + its JSONL path."""
    path = tmp_path / "state" / "healing.jsonl"
    return HealingLog(path), path


def read_rows(path: Path) -> list[dict]:
    """Parseable heal rows, oldest first; [] when the log was never written."""
    if not path.is_file():
        return []
    return [json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def failing_scripts(count: int, exc=None) -> list[StubMQTT]:
    """``count`` dials that all fail the handshake with ``exc``."""
    error = exc if exc is not None else ConnectionError("handshake refused")
    return [StubMQTT(connect_error=error) for _ in range(count)]


class TestListenReconnectHeal:
    """The MQTT listen reconnect loop: events, pacing, budget, non-failures."""

    def test_one_drop_reconnects_and_preserves_frames(
            self, monkeypatch, tmp_path, capsys):
        """One mid-listen drop -> one re-dial; frames accumulate across both
        sockets and exactly ONE redacted heal row is appended + mirrored."""
        clock = FakeClock()
        log, path = heal_log(tmp_path)
        dials = mqtt_seam(monkeypatch, clock, [
            StubMQTT(frames=[StubFrame()], drop=True, clock=clock),
            StubMQTT(frames=[StubFrame()], clock=clock),
        ])
        frames = MessengerService(StubSession()).listen(
            seconds=30.0, healing_log=log)

        assert [f["topic"] for f in frames] == ["/t_ms", "/t_ms"]
        assert [f["kind"] for f in frames] == ["PUBLISH", "PUBLISH"]
        assert [f["summary"] for f in frames] == [{"field": 1}, {"field": 1}]
        assert len(dials) == 2

        rows = read_rows(path)
        assert len(rows) == 1
        assert rows[0]["kind"] == KIND_REALTIME_RECONNECT
        assert rows[0]["trigger"] == "listen socket dropped"
        assert rows[0]["detail"] == "re-dial 1/3 after ConnectionError"
        assert "[heal] realtime-reconnect: listen socket dropped" \
            " — re-dial 1/3 after ConnectionError" in capsys.readouterr().err

    def test_reconnect_paces_through_the_session_governor(
            self, monkeypatch, tmp_path):
        """A re-dial is a fresh network dial: it passes the session
        transport's governor gate exactly once — and the FIRST dial does
        not (it is not governor-paced today, unchanged). With the gate
        reachable there is no bare sleep stacked on top of it."""
        clock = FakeClock()
        session = StubSession()
        governor = RecordingGovernor()
        session.transport = SimpleNamespace(governor=governor)
        log, path = heal_log(tmp_path)
        dials = mqtt_seam(monkeypatch, clock, [
            StubMQTT(frames=[StubFrame()], drop=True, clock=clock),
            StubMQTT(clock=clock),
        ])
        MessengerService(session).listen(seconds=30.0, healing_log=log)

        assert len(dials) == 2
        assert governor.gated == [False]
        assert clock.sleeps == []
        assert len(read_rows(path)) == 1

    def test_reconnect_without_a_governor_uses_the_fixed_backoff(
            self, monkeypatch, tmp_path):
        """No reachable governor gate (transport-less stub session): the
        re-dial still does not burst — one fixed REALTIME_RECONNECT_BACKOFF_S
        sleep per re-dial, recorded through the module ``time`` seam."""
        clock = FakeClock()
        log, _path = heal_log(tmp_path)
        dials = mqtt_seam(monkeypatch, clock, [
            StubMQTT(frames=[StubFrame()], drop=True, clock=clock),
            StubMQTT(clock=clock),
        ])
        MessengerService(StubSession()).listen(seconds=30.0, healing_log=log)

        assert len(dials) == 2
        assert clock.sleeps == [REALTIME_RECONNECT_BACKOFF_S]

    def test_reconnect_resumes_into_the_same_deadline_budget(
            self, monkeypatch, tmp_path):
        """The --seconds window is ONE budget for the whole session: a drop
        28s into a 30s window leaves the re-dial exactly 2s — the read
        timeout drawn from the remaining budget, never a fresh 30s window."""
        clock = FakeClock()
        log, _path = heal_log(tmp_path)
        dials = mqtt_seam(monkeypatch, clock, [
            StubMQTT(frames=[StubFrame()], drop=True,
                     advance_before_drop=28.0, clock=clock),
            StubMQTT(frames=[StubFrame()], clock=clock),
        ])
        frames = MessengerService(StubSession()).listen(
            seconds=30.0, healing_log=log)

        assert [f["topic"] for f in frames] == ["/t_ms", "/t_ms"]
        assert dials[0].read_timeouts == [5.0, 5.0]
        assert dials[1].read_timeouts[0] == pytest.approx(2.0)

    def test_persistent_failure_raises_original_after_budget_exhausted(
            self, monkeypatch, tmp_path, capsys):
        """Persistent handshake failure: the ORIGINAL ConnectionError
        propagates after exactly limit+1 = 4 pinned dials, every socket is
        torn down exactly once, and the log carries 3 re-dial rows plus the
        final 'reconnect budget exhausted' row."""
        clock = FakeClock()
        log, path = heal_log(tmp_path)
        dials = mqtt_seam(monkeypatch, clock, failing_scripts(4))
        with pytest.raises(ConnectionError, match="handshake refused"):
            MessengerService(StubSession()).listen(seconds=30.0,
                                                   healing_log=log)

        assert len(dials) == 4
        assert all(d.close_calls == 1 for d in dials)
        rows = read_rows(path)
        assert len(rows) == 4
        assert all(r["kind"] == KIND_REALTIME_RECONNECT for r in rows)
        assert all(r["trigger"] == "listen dial failed" for r in rows)
        assert [r["detail"] for r in rows[:3]] == [
            "re-dial 1/3 after ConnectionError",
            "re-dial 2/3 after ConnectionError",
            "re-dial 3/3 after ConnectionError",
        ]
        assert rows[3]["detail"] == \
            "reconnect budget exhausted (4 dial attempts)"
        assert "[heal] realtime-reconnect:" in capsys.readouterr().err

    def test_env_limit_caps_dials(self, monkeypatch, tmp_path):
        """FBK_HEAL_REALTIME_RECONNECTS=2 -> at most 3 dial attempts, with
        the final exhausted event carrying the spent count."""
        monkeypatch.setenv("FBK_HEAL_REALTIME_RECONNECTS", "2")
        clock = FakeClock()
        log, path = heal_log(tmp_path)
        dials = mqtt_seam(monkeypatch, clock, failing_scripts(3))
        with pytest.raises(ConnectionError, match="handshake refused"):
            MessengerService(StubSession()).listen(seconds=30.0,
                                                   healing_log=log)

        assert len(dials) == 3
        rows = read_rows(path)
        assert rows[-1]["detail"] == \
            "reconnect budget exhausted (3 dial attempts)"

    def test_env_value_clamps_to_the_hard_ceiling(self, monkeypatch, tmp_path):
        """A runaway override clamps at the healing engine's ceiling of 10 —
        11 dial attempts total, never more."""
        monkeypatch.setenv("FBK_HEAL_REALTIME_RECONNECTS", "50")
        assert realtime_reconnect_limit() == 10
        clock = FakeClock()
        log, _path = heal_log(tmp_path)
        dials = mqtt_seam(monkeypatch, clock, failing_scripts(11))
        with pytest.raises(ConnectionError, match="handshake refused"):
            MessengerService(StubSession()).listen(seconds=30.0,
                                                   healing_log=log)
        assert len(dials) == 11

    def test_env_zero_disables_reconnects(self, monkeypatch, tmp_path):
        """FBK_HEAL_REALTIME_RECONNECTS=0: healing on, but the budget is
        zero — single dial, zero rows, original error."""
        monkeypatch.setenv("FBK_HEAL_REALTIME_RECONNECTS", "0")
        clock = FakeClock()
        log, path = heal_log(tmp_path)
        dials = mqtt_seam(monkeypatch, clock, failing_scripts(2))
        with pytest.raises(ConnectionError, match="handshake refused"):
            MessengerService(StubSession()).listen(seconds=30.0,
                                                   healing_log=log)
        assert len(dials) == 1
        assert read_rows(path) == []

    def test_heal_off_is_the_pre_healing_single_dial(
            self, monkeypatch, tmp_path, capsys):
        """FBK_HEAL=off: byte-identical to the pre-healing listen — one
        dial, zero heal rows, no file created, nothing on stderr."""
        monkeypatch.setenv("FBK_HEAL", "off")
        clock = FakeClock()
        log, path = heal_log(tmp_path)
        dials = mqtt_seam(monkeypatch, clock, failing_scripts(2))
        with pytest.raises(ConnectionError, match="handshake refused"):
            MessengerService(StubSession()).listen(seconds=30.0,
                                                   healing_log=log)
        assert len(dials) == 1
        assert read_rows(path) == []
        assert not path.exists()
        assert "[heal]" not in capsys.readouterr().err

    def test_unwired_log_keeps_the_single_dial_behavior(
            self, monkeypatch, tmp_path):
        """Direct surface callers without a healing log (every in-tree
        caller but cmd_listen): no reconnect even with healing on — a
        reconnect without its audited event would be an unaccounted dial."""
        clock = FakeClock()
        _log, path = heal_log(tmp_path)
        dials = mqtt_seam(monkeypatch, clock, failing_scripts(2))
        with pytest.raises(ConnectionError, match="handshake refused"):
            MessengerService(StubSession()).listen(seconds=30.0)
        assert len(dials) == 1
        assert read_rows(path) == []

    def test_deadline_expiry_is_not_a_failure(
            self, monkeypatch, tmp_path, capsys):
        """The normal --seconds expiry returns the collected frames: no
        reconnect, no heal rows, nothing on stderr."""
        clock = FakeClock()
        log, path = heal_log(tmp_path)
        dials = mqtt_seam(monkeypatch, clock, [
            StubMQTT(frames=[StubFrame()], clock=clock),
        ])
        frames = MessengerService(StubSession()).listen(seconds=30.0,
                                                        healing_log=log)
        assert [f["topic"] for f in frames] == ["/t_ms"]
        assert len(dials) == 1
        assert read_rows(path) == []
        assert "[heal]" not in capsys.readouterr().err

    def test_keyboard_interrupt_never_reconnects_or_logs(
            self, monkeypatch, tmp_path, capsys):
        """A user interrupt is not a failure: it propagates untouched, the
        socket is still torn down once, no re-dial, no heal rows."""
        clock = FakeClock()
        log, path = heal_log(tmp_path)
        dials = mqtt_seam(monkeypatch, clock, [
            StubMQTT(read_error=KeyboardInterrupt(), clock=clock),
        ])
        with pytest.raises(KeyboardInterrupt):
            MessengerService(StubSession()).listen(seconds=30.0,
                                                   healing_log=log)
        assert len(dials) == 1
        assert dials[0].close_calls == 1
        assert read_rows(path) == []
        assert "[heal]" not in capsys.readouterr().err


class TestListenDgwReconnectHeal:
    """The DGW listen heals through the same driver (both transports)."""

    def test_dgw_drop_reconnects_and_preserves_frames(
            self, monkeypatch, tmp_path, capsys):
        """One mid-listen DGW drop -> one re-dial; responses accumulate
        across both sockets; exactly one heal row; the subscribe set is
        replayed on the fresh socket."""
        clock = FakeClock()
        log, path = heal_log(tmp_path)
        dials = dgw_seam(monkeypatch, clock, [
            StubDGW(responses=[StubResponse()], drop=True, clock=clock),
            StubDGW(responses=[StubResponse()], clock=clock),
        ])
        frames = MessengerService(StubSession()).listen_dgw(
            seconds=30.0, healing_log=log)

        assert frames == [
            {"request_id": 7, "payload_type": "state_sync_result",
             "payload": '{"sync":"ok"}'},
            {"request_id": 7, "payload_type": "state_sync_result",
             "payload": '{"sync":"ok"}'},
        ]
        assert len(dials) == 2
        assert dials[0].subscribed_threads == [DGW_DEFAULT_THREAD_ID]
        assert dials[1].subscribed_threads == [DGW_DEFAULT_THREAD_ID]

        rows = read_rows(path)
        assert len(rows) == 1
        assert rows[0]["kind"] == KIND_REALTIME_RECONNECT
        assert rows[0]["trigger"] == "listen socket dropped"
        assert rows[0]["detail"] == "re-dial 1/3 after ConnectionError"
        assert "[heal] realtime-reconnect:" in capsys.readouterr().err

    def test_dgw_persistent_failure_budget_exhausted(
            self, monkeypatch, tmp_path):
        """Persistent DGW handshake failure: original DGWError-family raise
        (a ConnectionError here), limit+1 dials, exhausted event last."""
        clock = FakeClock()
        log, path = heal_log(tmp_path)
        dials = dgw_seam(monkeypatch, clock, [
            StubDGW(connect_error=ConnectionError("dgw handshake refused"))
            for _ in range(4)
        ])
        with pytest.raises(ConnectionError, match="dgw handshake refused"):
            MessengerService(StubSession()).listen_dgw(seconds=30.0,
                                                       healing_log=log)
        assert len(dials) == 4
        rows = read_rows(path)
        assert len(rows) == 4
        assert rows[-1]["detail"] == \
            "reconnect budget exhausted (4 dial attempts)"
