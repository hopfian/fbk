"""``fbk feed publish-batch`` — the sequential multi-post run.

Unit (offline): 3 texts → 3 FeedService.publish calls in order with the
distinct texts and the parsed privacy; fail-soft on a middle typed
error (post 2 raises RateLimitedError → posts 1 and 3 still publish,
the report shows all three, exit 1); GovernorBlockedError is FATAL (the
cap is the cap — the remaining posts are skipped, never attempted,
exit 1); the --batch-gap jitter rides the shared page-gap helper; and
the min-2 precondition. All driven through the REAL app parser under
run_command with the service seam stubbed (commands.feed.new_session /
FeedService — the monkeypatch seam the with_session docstring
documents). Nothing here touches the network or a real Session.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fakes import StubSession

import commands.feed as feed_cmd
from app import build_parser
from commands.common import run_command
from domain.common import Privacy
from governor import GovernorBlockedError
from graphql.errors import RateLimitedError


def _clear_env(monkeypatch) -> None:
    monkeypatch.delenv("FBK_ROOT", raising=False)
    monkeypatch.delenv("FBK_COOKIES", raising=False)
    monkeypatch.delenv("FBK_IMPERSONATE", raising=False)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    _clear_env(monkeypatch)


def _run(capsys, argv: list[str]) -> tuple[int, str, str, dict[str, Any]]:
    """Drive one feed subcommand through the real app parser under
    run_command; return (rc, stdout, stderr, payload) — the payload
    parses from the trailing compact JSON line (the emit contract)."""
    args = build_parser().parse_args(argv)
    rc = run_command(args.fn, args)
    captured = capsys.readouterr()
    out = captured.out
    if not out.strip():
        return rc, out, captured.err, {}
    try:
        return rc, out, captured.err, json.loads(out)
    except json.JSONDecodeError:
        return rc, out, captured.err, json.loads(out.splitlines()[-1])


class _ScriptedService:
    """FeedService stub: records publish calls, plays scripted outcomes
    (a response dict or an exception to raise at that ordinal)."""

    def __init__(self, session: Any, calls: list, outcomes: list) -> None:
        self.session = session
        self._calls = calls
        self._outcomes = list(outcomes)

    def publish(self, text: str, privacy: Privacy,
                **kw: Any) -> dict[str, Any]:
        self._calls.append((text, privacy, kw))
        outcome = self._outcomes[len(self._calls) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return {"data": {"story_create": {"feed_story_edge": {
            "node": {"post_id": f"p{len(self._calls)}"}}}}}


def _install(monkeypatch, outcomes: list) -> list:
    """Stub the service seam in commands.feed; returns the shared call
    log the scripted service records into."""
    calls: list = []

    def make_service(session: Any) -> _ScriptedService:
        return _ScriptedService(session, calls, outcomes)

    monkeypatch.setattr(feed_cmd, "FeedService", make_service)
    monkeypatch.setattr(feed_cmd, "new_session", lambda args: StubSession())
    return calls


def _batch_argv(texts: list[str], *extra: str) -> list[str]:
    argv = ["feed", "publish-batch"]
    for t in texts:
        argv += ["--text", t]
    return argv + list(extra)


class TestParserShape:
    """--text is repeatable (append) with min 2 enforced in the handler,
    --privacy defaults to friends, --batch-gap to 0 (governor paces)."""

    def test_texts_parse_in_order_as_a_list(self):
        args = build_parser().parse_args(
            ["feed", "publish-batch", "--text", "a", "--text", "b",
             "--text", "c"])
        assert callable(args.fn)
        assert args.text == ["a", "b", "c"]
        assert args.privacy == "friends"
        assert args.batch_gap == 0.0

    def test_below_two_texts_is_a_failed_precondition(self, capsys, monkeypatch):
        _install(monkeypatch, [None])
        rc, _, err, _ = _run(capsys, _batch_argv(["only one"]))
        assert rc == 1
        assert "at least 2 --text posts" in err


class TestBatchSuccess:
    """3 texts → exactly 3 sequential publish calls, one results array
    entry per text, exit 0."""

    def test_three_texts_three_calls_full_report(self, capsys, monkeypatch):
        calls = _install(monkeypatch, [None, None, None])
        rc, out, _, payload = _run(
            capsys, _batch_argv(["first post", "second post", "third post"],
                                "--privacy", "public"))
        assert rc == 0
        # 3 service calls, sequential in argv order, one privacy for all
        assert [c[0] for c in calls] == ["first post", "second post",
                                          "third post"]
        assert all(c[1] is Privacy.PUBLIC for c in calls)
        assert [r["status"] for r in payload["results"]] == [
            "published", "published", "published"]
        assert [r["post_id"] for r in payload["results"]] == ["p1", "p2", "p3"]
        assert payload["published"] == 3
        assert payload["failed"] == 0
        assert payload["aborted"] is False
        assert "summary: 3 published, 0 failed, 0 skipped" in out

    def test_default_privacy_is_friends(self, capsys, monkeypatch):
        calls = _install(monkeypatch, [None, None])
        rc, _, _, _ = _run(capsys, _batch_argv(["a", "b"]))
        assert rc == 0
        assert all(c[1] is Privacy.FRIENDS for c in calls)


class TestFailSoft:
    """A typed error on post k does NOT abort the batch: the remaining
    posts still publish and the report carries every text — but the run
    exits 1 (any failed → 1)."""

    def test_middle_rate_limit_keeps_going(self, capsys, monkeypatch):
        calls = _install(monkeypatch, [
            None, RateLimitedError("soft block"), None])
        rc, out, _err, payload = _run(
            capsys, _batch_argv(["one", "two", "three"]))
        assert rc == 1
        assert [c[0] for c in calls] == ["one", "two", "three"]
        [r1, r2, r3] = payload["results"]
        assert r1["status"] == "published" and r1["post_id"] == "p1"
        assert r2["status"] == "failed"
        assert r2["error_type"] == "RateLimitedError"
        assert "soft block" in r2["error"]
        assert r3["status"] == "published" and r3["post_id"] == "p3"
        assert payload["published"] == 2
        assert payload["failed"] == 1
        assert payload["aborted"] is False
        assert "1 failed" in out

    def test_json_emits_the_results_array(self, capsys, monkeypatch):
        _install(monkeypatch, [None, RateLimitedError("x"), None])
        rc, out, _, payload = _run(
            capsys, _batch_argv(["a", "b", "c"], "--json"))
        assert rc == 1
        assert json.loads(out) == payload  # machine mode: JSON alone
        assert [r["status"] for r in payload["results"]] == [
            "published", "failed", "published"]


class TestGovernorBlockedIsFatal:
    """GovernorBlockedError is a STOP signal, not a per-post failure —
    the batch aborts immediately and the remainder is marked skipped
    without ever being attempted (the cap is the cap)."""

    def test_block_on_second_post_skips_the_third(self, capsys, monkeypatch):
        calls = _install(monkeypatch, [
            None, GovernorBlockedError("mutation budget exhausted"), None])
        rc, out, _, payload = _run(capsys, _batch_argv(["a", "b", "c"]))
        assert rc == 1
        assert [c[0] for c in calls] == ["a", "b"]  # "c" never attempted
        assert [r["status"] for r in payload["results"]] == [
            "published", "governor_blocked", "skipped"]
        assert payload["aborted"] is True
        assert "mutation budget exhausted" in payload["governor_error"]
        assert payload["skipped"] == 1
        assert "governor aborted the batch" in out


class TestBatchGap:
    """--batch-gap adds an EXTRA jittered sleep between posts (±50%, the
    docs/11 §3 jitter) on top of the governor's own pacing; the default
    0 adds none."""

    def test_gap_sleeps_between_posts_jittered(self, capsys, monkeypatch):
        _install(monkeypatch, [None, None, None])
        sleeps: list[float] = []
        monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))
        rc, _, _, _ = _run(capsys, _batch_argv(["a", "b", "c"],
                                               "--batch-gap", "5.0"))
        assert rc == 0
        assert len(sleeps) == 2  # between 1→2 and 2→3, never before the first
        assert all(2.5 <= s <= 7.5 for s in sleeps)

    def test_default_gap_adds_no_extra_sleeps(self, capsys, monkeypatch):
        _install(monkeypatch, [None, None, None])
        sleeps: list[float] = []
        monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))
        rc, _, _, _ = _run(capsys, _batch_argv(["a", "b", "c"]))
        assert rc == 0
        assert sleeps == []  # the governor (stubbed out) paces, not the command
