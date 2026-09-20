"""Transport-level self-healing: connection-phase retry pins (offline).

Pins FBTransport's read-only connection-phase retry (the src/healing.py
policy, docs/11 §5 double-post doctrine): governed reads — ``get()`` and
``post_graphql()`` with ``is_mutation=False`` — retry curl_cffi
``Timeout``/``ConnectionError`` failures up to
``FBK_HEAL_TRANSPORT_RETRIES`` times per call (default 1, ceiling 3;
``FBK_HEAL=off`` disables), each retry preceded by one ``transport-retry``
row in the attached HealingLog. Mutations, form POSTs (``post()``), and
the raw seam are never retried; non-connection-phase RequestException
errors never retry. The governor gate debits ONCE per logical call (a
retry must not re-pace); the journal note records the attempt that
actually completed. All offline: the curl session is a scripted fake
(same stubbing pattern as test_transport_offline.py).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from curl_cffi import requests as creq

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fakes import StubTransportResponse

from healing import KIND_TRANSPORT_RETRY, HealingLog
from journal.recorder import JSONLJournal
from transport.profile import ClientProfile
from transport.session import FBTransport

GRAPHQL_URL = "https://www.facebook.com/api/graphql/"
PAGE_URL = "https://www.facebook.com/"


class RecordingGovernor:
    """Records governor gate invocations (docs/10 §7 wiring)."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def before_request(self, *, is_mutation: bool = False, quiet: bool = False) -> None:
        self.calls.append(("before", is_mutation))

    def observe_soft_block(self) -> None:
        self.calls.append(("soft_block",))


class ScriptedHTTPSession:
    """Replaces FBTransport._session: one scripted outcome per call.

    Outcomes are response-like objects (returned) or Exception instances
    (raised at call time, simulating a transport failure).
    """

    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple] = []

    def _next(self, method: str, url: str, kw: dict[str, Any]) -> Any:
        self.calls.append((method, url, kw))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def get(self, url: str, **kw: Any) -> Any:
        return self._next("GET", url, kw)

    def post(self, url: str, **kw: Any) -> Any:
        return self._next("POST", url, kw)


def make_transport(tmp_path: Path, outcomes: list[Any],
                   *, governor: Any = None) -> FBTransport:
    transport = FBTransport(
        {"c_user": "1", "xs": "SECRET-XS", "datr": "SECRET-DATR"},
        profile=ClientProfile(),
        journal=JSONLJournal(tmp_path / "j.jsonl"),
        timeout=5, impersonate="chrome136", governor=governor)
    transport._session = ScriptedHTTPSession(outcomes)
    return transport


def heal_rows(path: Path) -> list[dict[str, Any]]:
    """Every JSONL row in a healing log file, oldest first."""
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line]


@pytest.fixture(autouse=True)
def default_heal_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the healing policy env to defaults for every test.

    ``healing_enabled()``/``transport_retry_limit()`` read the
    environment at call time, so each test starts from unset
    ``FBK_HEAL``/``FBK_HEAL_TRANSPORT_RETRIES`` (healing on, one retry)
    and opts out explicitly where the scenario demands it.
    """
    monkeypatch.delenv("FBK_HEAL", raising=False)
    monkeypatch.delenv("FBK_HEAL_TRANSPORT_RETRIES", raising=False)


class TestTransportRetry:
    """Pins the connection-phase retry semantics on governed reads."""

    def test_read_post_retried_once_on_timeout_then_succeeds(self, tmp_path):
        log_path = tmp_path / "healing.jsonl"
        gov = RecordingGovernor()
        transport = make_transport(
            tmp_path,
            [creq.exceptions.Timeout("attempt 1 lost"),
             StubTransportResponse(text="ok", url=GRAPHQL_URL)],
            governor=gov)
        transport.healing_log = HealingLog(log_path)
        resp = transport.post_graphql(
            GRAPHQL_URL, data={"doc_id": "123456789"},
            friendly_name="CometRead", lsd=None)
        assert resp.text == "ok"  # no exception: the retried send won
        assert len(transport._session.calls) == 2  # exactly one retry
        assert gov.calls == [("before", False)]  # gate debited once, not per attempt
        rows = heal_rows(log_path)
        assert len(rows) == 1
        assert rows[0]["kind"] == KIND_TRANSPORT_RETRY
        assert rows[0]["trigger"] == "connection-phase failure on POST graphql/CometRead"
        assert rows[0]["detail"] == "Timeout; read-only retry"
        entries = JSONLJournal(tmp_path / "j.jsonl").read_all()
        assert len(entries) == 1  # the second (successful) attempt is journaled
        assert entries[0]["status"] == 200

    def test_mutation_post_is_never_retried(self, tmp_path):
        log_path = tmp_path / "healing.jsonl"
        transport = make_transport(
            tmp_path, [creq.exceptions.Timeout("response lost")])
        transport.healing_log = HealingLog(log_path)
        with pytest.raises(creq.exceptions.Timeout):
            transport.post_graphql(
                GRAPHQL_URL, data={"doc_id": "987654321"},
                friendly_name="PublishMutation", lsd=None, is_mutation=True)
        assert len(transport._session.calls) == 1  # no second send
        assert heal_rows(log_path) == []  # and no heal row

    def test_retry_limit_zero_disables_retry(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FBK_HEAL", "off")  # master switch: every heal off
        log_path = tmp_path / "healing.jsonl"
        transport = make_transport(
            tmp_path,
            [creq.exceptions.Timeout("lost"),
             StubTransportResponse(text="unreachable")])
        transport.healing_log = HealingLog(log_path)
        with pytest.raises(creq.exceptions.Timeout):
            transport.get(PAGE_URL)
        assert len(transport._session.calls) == 1  # no second send
        assert heal_rows(log_path) == []

    def test_connection_error_on_get_is_retried(self, tmp_path):
        log_path = tmp_path / "healing.jsonl"
        transport = make_transport(
            tmp_path,
            [creq.exceptions.ConnectionError("connection refused"),
             StubTransportResponse(text="page", url=PAGE_URL)])
        transport.healing_log = HealingLog(log_path)
        resp = transport.get(PAGE_URL)
        assert resp.text == "page"
        assert len(transport._session.calls) == 2
        rows = heal_rows(log_path)
        assert len(rows) == 1
        assert rows[0]["kind"] == KIND_TRANSPORT_RETRY
        assert rows[0]["trigger"] == "connection-phase failure on GET"
        assert rows[0]["detail"] == "ConnectionError; read-only retry"

    def test_generic_request_exception_is_not_retried(self, tmp_path):
        log_path = tmp_path / "healing.jsonl"
        transport = make_transport(
            tmp_path, [creq.exceptions.RequestException("boom")])
        transport.healing_log = HealingLog(log_path)
        with pytest.raises(creq.exceptions.RequestException):
            transport.get(PAGE_URL)
        assert len(transport._session.calls) == 1  # connection-phase filter holds
        assert heal_rows(log_path) == []

    def test_retries_exhausted_propagates_and_records_one_row(self, tmp_path):
        log_path = tmp_path / "healing.jsonl"
        transport = make_transport(
            tmp_path,
            [creq.exceptions.Timeout("first failure"),
             creq.exceptions.Timeout("second failure")])
        transport.healing_log = HealingLog(log_path)
        with pytest.raises(creq.exceptions.Timeout) as excinfo:
            transport.get(PAGE_URL)
        assert "second failure" in str(excinfo.value)  # most recent failure
        assert len(transport._session.calls) == 2  # the original plus one retry
        rows = heal_rows(log_path)
        assert len(rows) == 1  # one row: logged before the single retry only
        assert rows[0]["kind"] == KIND_TRANSPORT_RETRY
        assert rows[0]["detail"] == "Timeout; read-only retry"

    def test_limit_two_retries_twice_then_succeeds(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FBK_HEAL_TRANSPORT_RETRIES", "2")
        log_path = tmp_path / "healing.jsonl"
        transport = make_transport(
            tmp_path,
            [creq.exceptions.Timeout("a"),
             creq.exceptions.ConnectionError("b"),
             StubTransportResponse(text="ok", url=PAGE_URL)])
        transport.healing_log = HealingLog(log_path)
        resp = transport.get(PAGE_URL)
        assert resp.text == "ok"
        assert len(transport._session.calls) == 3  # honored beyond the default 1
        rows = heal_rows(log_path)
        assert [row["detail"] for row in rows] == [
            "Timeout; read-only retry", "ConnectionError; read-only retry"]

    def test_retry_proceeds_without_a_healing_log(self, tmp_path):
        transport = make_transport(
            tmp_path,
            [creq.exceptions.Timeout("lost"),
             StubTransportResponse(text="ok", url=PAGE_URL)])
        transport.healing_log = None  # the default: no Session wiring
        resp = transport.get(PAGE_URL)  # no crash; the retry still happens
        assert resp.text == "ok"
        assert len(transport._session.calls) == 2

    def test_form_post_is_never_retried(self, tmp_path):
        log_path = tmp_path / "healing.jsonl"
        transport = make_transport(
            tmp_path, [creq.exceptions.Timeout("lost")])
        transport.healing_log = HealingLog(log_path)
        with pytest.raises(creq.exceptions.Timeout):
            transport.post(PAGE_URL, data={"fb_dtsg": "TOKEN"})
        assert len(transport._session.calls) == 1  # post() stays unwrapped
        assert heal_rows(log_path) == []
