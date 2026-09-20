"""Retry policy + feed pagination commands + overview aggregate tests.

Offline only — every network shape is a canned StubSession response or
the real captured fixtures (feed_page1). Covers docs/10 §7 (backoff),
docs/11 §5 (pacing) and the docs/15 cheap-reads dashboard.
"""
from __future__ import annotations

import argparse
import json

import pytest
from fakes import StubSession

import commands.feed as feed_commands
import commands.overview as overview_commands
import retry as retry_module
from commands.common import run_command
from commands.feed import cmd_paginate, cmd_read
from commands.overview import cmd_overview
from graphql.errors import NotLoggedInError, RateLimitedError
from retry import RetryPolicy, run_with_retry
from surfaces.overview import OVERVIEW_KEYS, OverviewService

STUB_USER_ID = "12345678901234"


def _ns(**kw) -> argparse.Namespace:
    """A minimal command namespace (defaults mirror the parsers)."""
    base = {"retry": 0, "as_json": False, "no_journal": False,
            "pages": 1, "page_gap": 2.0}
    base.update(kw)
    return argparse.Namespace(**base)


# A minimal shape-faithful page-2 payload (same skeleton as
# tests/unit/test_surface_feed.py): one Story + page_info to chain on.
PAGE2_PAYLOAD = {
    "data": {
        "node": {
            "__typename": "Story",
            "id": "UzpfSUZTOjE6LTAxMDEwMTAxMDEwMTAxMDEwMTAxOkppbQ",
            "creation_time": 1789712173,
            "actors": [{"__typename": "User", "id": "12345678901234568",
                        "name": "Page Two Author"}],
            "feedback": {"id": "ZmVlZGJhY2s6MTUzMjkzMDQ1ODg4MTY4Mw"},
            "comet_sections": {"content": {"story": {"message": {"text": {
                "__typename": "TextWithEntities",
                "text": "page two headline"}}}}},
            "permalink_url": "https://www.facebook.com/story.php",
        },
        "page_info": {"__typename": "PageInfo", "end_cursor": "PAGE2CURSOR",
                      "has_next_page": True},
    }
}

# Same page minus the pagination plumbing: the walk stops here.
PAGE_LAST_PAYLOAD = json.loads(json.dumps(PAGE2_PAYLOAD))
PAGE_LAST_PAYLOAD["data"].pop("page_info")


# --------------------------------------------------------------------- retry
class TestRetryPolicyDelay:
    """Pins exponential backoff growth, the max-delay cap, jitter bounds,
    and the 0.25s floor (docs/10 §7)."""

    def test_exponential_growth_cv_zero(self):
        policy = RetryPolicy(base_delay_s=2.0, max_delay_s=30.0, jitter_cv=0.0)
        assert [policy.delay_for(a) for a in range(4)] == [2.0, 4.0, 8.0, 16.0]

    def test_cap_at_max_delay(self):
        policy = RetryPolicy(base_delay_s=2.0, max_delay_s=30.0, jitter_cv=0.0)
        assert policy.delay_for(4) == 30.0
        assert policy.delay_for(10) == 30.0

    def test_jitter_bounds(self):
        policy = RetryPolicy(base_delay_s=4.0, max_delay_s=30.0, jitter_cv=0.5)
        for attempt in range(4):
            nominal = min(4.0 * (2 ** attempt), 30.0)
            for _ in range(40):
                assert 0.5 * nominal <= policy.delay_for(attempt) <= 1.5 * nominal

    def test_jitter_floor_never_below_quarter_second(self):
        policy = RetryPolicy(base_delay_s=0.1, max_delay_s=1.0, jitter_cv=0.5)
        for _ in range(40):
            assert policy.delay_for(0) >= 0.25


class TestRunWithRetry:
    """Pins run_with_retry semantics: matched errors retry with the
    policy's own delays, everything else raises immediately."""

    def test_succeeds_first_try_no_sleep(self):
        sleeps: list[float] = []
        result = run_with_retry(lambda: "ok", RetryPolicy(), sleeper=sleeps.append)
        assert result == "ok"
        assert sleeps == []

    def test_retries_rate_limit_then_succeeds(self):
        policy = RetryPolicy(max_retries=2, base_delay_s=2.0, jitter_cv=0.0)
        sleeps: list[float] = []
        calls = []

        def fn():
            calls.append(1)
            if len(calls) == 1:
                raise RateLimitedError("soft block")
            return "recovered"

        assert run_with_retry(fn, policy, sleeper=sleeps.append) == "recovered"
        assert len(calls) == 2
        # the sleeper got exactly the policy's delays (attempt 0)
        assert sleeps == [policy.delay_for(0)]

    def test_exhausts_retries_raises_last(self):
        policy = RetryPolicy(max_retries=2, base_delay_s=2.0, jitter_cv=0.0)
        sleeps: list[float] = []
        calls = []

        def fn():
            calls.append(1)
            raise RateLimitedError("still blocked")

        with pytest.raises(RateLimitedError):
            run_with_retry(fn, policy, sleeper=sleeps.append)
        # 1 initial attempt + 2 retries -> 2 sleeps (attempts 0 and 1)
        assert len(calls) == 3
        assert sleeps == [policy.delay_for(0), policy.delay_for(1)]

    def test_non_matching_error_raised_immediately(self):
        sleeps: list[float] = []
        calls = []

        def fn():
            calls.append(1)
            raise NotLoggedInError("session expired")

        with pytest.raises(NotLoggedInError):
            run_with_retry(fn, RetryPolicy(), sleeper=sleeps.append)
        assert len(calls) == 1
        assert sleeps == []

    def test_sleeper_sees_policy_delays(self):
        """The injected sleeper proves the delays were the policy's own."""
        policy = RetryPolicy(max_retries=3, base_delay_s=1.0, max_delay_s=4.0,
                             jitter_cv=0.0)
        sleeps: list[float] = []
        calls = []

        def fn():
            calls.append(1)
            if len(calls) <= 3:
                raise RateLimitedError("backing off")
            return "done"

        assert run_with_retry(fn, policy, sleeper=sleeps.append) == "done"
        assert sleeps == [1.0, 2.0, 4.0]  # capped at max on attempt 2

    def test_default_sleeper_resolved_at_call_time(self, monkeypatch):
        """run_with_retry's fallback sleeper is looked up per call, so a
        global monkeypatch of fbk.retry.time.sleep takes effect."""
        real_sleep = []
        monkeypatch.setattr(retry_module.time, "sleep", real_sleep.append)
        calls = []

        def fn():
            calls.append(1)
            if len(calls) == 1:
                raise RateLimitedError("soft block")
            return "ok"

        assert run_with_retry(fn, RetryPolicy(max_retries=1)) == "ok"
        assert len(real_sleep) == 1


# ------------------------------------------------- run_command --retry wiring
class TestRunCommandRetryWiring:
    """Pins the run_command --retry wiring: exact retry counts, the
    rate-limit exit code (5), and fail-fast defaults."""

    def test_retries_then_succeeds(self, monkeypatch, capsys):
        calls = []

        def fn(args):
            calls.append(1)
            if len(calls) == 1:
                raise RateLimitedError("transient soft block")
            return 0

        sleeps: list[float] = []
        monkeypatch.setattr(retry_module.time, "sleep", sleeps.append)
        rc = run_command(fn, _ns(retry=1))
        assert rc == 0
        assert len(calls) == 2  # initial + exactly 1 retry
        assert len(sleeps) == 1

    def test_retry_exhausted_maps_to_rate_limit_exit_code(self, monkeypatch):
        calls = []

        def fn(args):
            calls.append(1)
            raise RateLimitedError("persistent soft block")

        monkeypatch.setattr(retry_module.time, "sleep", lambda s: None)
        rc = run_command(fn, _ns(retry=2))
        assert rc == 5
        assert len(calls) == 3  # 1 initial + 2 retries

    def test_default_zero_retries_fails_fast(self, monkeypatch):
        calls = []

        def fn(args):
            calls.append(1)
            raise RateLimitedError("soft block")

        rc = run_command(fn, _ns(retry=0))
        assert rc == 5
        assert len(calls) == 1

    def test_other_typed_errors_never_retried(self, monkeypatch):
        calls = []

        def fn(args):
            calls.append(1)
            raise NotLoggedInError("logged out")

        monkeypatch.setattr(retry_module.time, "sleep", lambda s: None)
        rc = run_command(fn, _ns(retry=3))
        assert rc == 3
        assert len(calls) == 1

    def test_keyboard_interrupt_still_130(self):
        def fn(args):
            raise KeyboardInterrupt
        assert run_command(fn, _ns(retry=2)) == 130


# -------------------------------------------------------- feed page walking
class TestFeedReadPages:
    """Pins the multi-page feed walk: cursor chaining off page 1's real
    end_cursor, the jittered inter-page pacing sleep, and the dry-stream
    early stop."""

    def test_two_page_walk_chains_and_sleeps(self, monkeypatch, capsys,
                                              feed_page1):
        stub = StubSession(responses={
            "CometModernHomeFeedQuery": feed_page1,
            "CometNewsFeedPaginationQuery": PAGE2_PAYLOAD,
        })
        monkeypatch.setattr(feed_commands, "new_session", lambda a: stub)
        sleeps: list[float] = []
        monkeypatch.setattr(feed_commands.time, "sleep", sleeps.append)

        rc = cmd_read(_ns(command="feed", feed_command="read", pages=2,
                          page_gap=2.0, as_json=True))
        assert rc == 0

        friendlies = [c[0] for c in stub.graphql.calls]
        assert friendlies == ["CometModernHomeFeedQuery",
                              "CometNewsFeedPaginationQuery"]
        # page 2 chained off page 1's real end_cursor
        _, _, variables = stub.graphql.calls[1]
        assert variables["cursor"]  # non-empty cursor token passed through

        # exactly one inter-page gap, jittered 2.0s +-50%
        assert len(sleeps) == 1
        assert 1.0 <= sleeps[0] <= 3.0

        out = json.loads(capsys.readouterr().out)
        assert out["pages"] == 2
        assert out["end_cursor"] == "PAGE2CURSOR"
        assert out["has_next_page"] is True
        # aggregated: page 1 stories + the one page-2 story
        assert len(out["stories"]) > 1
        assert any(s["actor"] == "Page Two Author" for s in out["stories"])

    def test_single_page_default_makes_one_call(self, monkeypatch, capsys,
                                                feed_page1):
        stub = StubSession(responses={"CometModernHomeFeedQuery": feed_page1})
        monkeypatch.setattr(feed_commands, "new_session", lambda a: stub)
        sleeps: list[float] = []
        monkeypatch.setattr(feed_commands.time, "sleep", sleeps.append)

        rc = cmd_read(_ns(command="feed", feed_command="read",
                          as_json=True))
        assert rc == 0
        assert len(stub.graphql.calls) == 1
        assert sleeps == []
        out = json.loads(capsys.readouterr().out)
        assert out["pages"] == 1

    def test_stops_early_when_stream_runs_dry(self, monkeypatch, capsys,
                                              feed_page1):
        stub = StubSession(responses={
            "CometModernHomeFeedQuery": feed_page1,
            "CometNewsFeedPaginationQuery": PAGE_LAST_PAYLOAD,
        })
        monkeypatch.setattr(feed_commands, "new_session", lambda a: stub)
        sleeps: list[float] = []
        monkeypatch.setattr(feed_commands.time, "sleep", sleeps.append)

        rc = cmd_read(_ns(command="feed", feed_command="read", pages=5,
                          page_gap=2.0, as_json=True))
        assert rc == 0
        # asked for 5, walked 2: page 2 carried no cursor
        assert len(stub.graphql.calls) == 2
        assert len(sleeps) == 1
        out = json.loads(capsys.readouterr().out)
        assert out["pages"] == 2
        assert out["end_cursor"] is None
        assert out["has_next_page"] is False


class TestFeedPaginateCommand:
    """Pins the ``feed paginate`` command: cursor passthrough, limit
    slicing, and next-cursor emission."""

    def test_paginate_emits_stories_and_next_cursor(self, monkeypatch, capsys):
        stub = StubSession(
            responses={"CometNewsFeedPaginationQuery": PAGE2_PAYLOAD})
        monkeypatch.setattr(feed_commands, "new_session", lambda a: stub)

        rc = cmd_paginate(_ns(command="feed", feed_command="paginate",
                              cursor="PAGE1CURSOR", limit=None, as_json=True))
        assert rc == 0

        friendly, _, variables = stub.graphql.calls[-1]
        assert friendly == "CometNewsFeedPaginationQuery"
        assert variables["cursor"] == "PAGE1CURSOR"

        out = json.loads(capsys.readouterr().out)
        assert out["next_cursor"] == "PAGE2CURSOR"
        assert out["page"]["end_cursor"] == "PAGE2CURSOR"
        assert out["page"]["has_next_page"] is True
        assert len(out["page"]["stories"]) == 1
        assert out["page"]["stories"][0]["actor"] == "Page Two Author"

    def test_paginate_limit_slices_stories(self, monkeypatch, capsys):
        stub = StubSession(
            responses={"CometNewsFeedPaginationQuery": PAGE2_PAYLOAD})
        monkeypatch.setattr(feed_commands, "new_session", lambda a: stub)

        rc = cmd_paginate(_ns(command="feed", feed_command="paginate",
                              cursor="C", limit=0, as_json=True))
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["page"]["stories"] == []
        assert out["next_cursor"] == "PAGE2CURSOR"  # cursor still chains


# ------------------------------------------------------------ overview
class TestOverviewCollect:
    """Pins the overview aggregate: stable keys, per-component error
    isolation, and empty-session degradation to empty reads."""

    def _canned(self, feed_page1, overrides=None):
        responses = {
            "CometNotificationsBadgeCountQuery": {
                "data": {"viewer": {"notifications_unseen_count": 3}}},
            "useCometWatchBadgeCountQuery": {
                "data": {"viewer": {"bookmarks": {"edges": [
                    {"node": {"unread_count": 7}}]}}}},
            "useFBChatVisibility_PresenceStatusChatVisibilityQuery": {
                "data": {"viewer": {"chat_visibility": True}}},
            "CometModernHomeFeedQuery": feed_page1,
            "MWCMBlendedThreadListQuery": {
                "data": {"viewer": {"message_threads": {
                    "nodes": [{"__typename": "MessengerThread",
                               "id": "12345678901234560", "name": "Alice"}]}}}},
        }
        if overrides:
            responses.update(overrides)
        return StubSession(responses=responses)

    def test_stable_keys_and_values(self, feed_page1):
        data = OverviewService(self._canned(feed_page1)).collect()
        assert set(data) == set(OVERVIEW_KEYS)

        assert data["identity"] == {"user_id": STUB_USER_ID,
                                    "user_name": "Test User",
                                    "state": "logged_in",
                                    "revision": "1047868043"}
        assert data["notifications_badge"] == 3
        assert data["watch_badge"] == 7
        assert data["presence"] == {"chat_visibility": True}
        assert data["thread_count"] == 1
        assert isinstance(data["registry_size"], int) \
            and data["registry_size"] > 0

        feed = data["feed_head"]
        assert feed["count"] >= 1
        assert len(feed["heads"]) <= 3
        assert feed["has_next_page"] is True

    def test_one_failing_surface_yields_error_key_only_there(self, feed_page1):
        stub = self._canned(
            feed_page1,
            {"CometNotificationsBadgeCountQuery": NotLoggedInError("expired")})
        data = OverviewService(stub).collect()
        assert set(data) == set(OVERVIEW_KEYS)
        assert data["notifications_badge"] == {"error": "NotLoggedInError: expired"}
        # every other component still collected fine
        assert data["watch_badge"] == 7
        assert data["presence"] == {"chat_visibility": True}
        assert data["thread_count"] == 1
        assert "error" not in data["identity"]

    def test_bootstrap_only_session_identity_survives(self):
        stub = StubSession(responses={})
        stub.graphql.strict = False
        data = OverviewService(stub).collect()
        assert set(data) == set(OVERVIEW_KEYS)
        ident = data["identity"]
        assert "error" not in ident
        assert ident["user_id"] == STUB_USER_ID
        assert ident["user_name"] == "Test User"
        # soft surfaces degrade to empty reads, not missing keys
        assert data["notifications_badge"] == 0
        assert data["thread_count"] == 0
        assert data["feed_head"]["count"] == 0

    def test_command_emits_dict(self, monkeypatch, capsys, feed_page1):
        stub = self._canned(feed_page1)
        monkeypatch.setattr(overview_commands, "new_session", lambda a: stub)
        rc = cmd_overview(_ns(command="overview", as_json=True))
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert set(out) == set(OVERVIEW_KEYS)
        assert out["notifications_badge"] == 3

    def test_command_human_dashboard_prints_every_key(self, monkeypatch,
                                                       capsys, feed_page1):
        stub = self._canned(feed_page1)
        monkeypatch.setattr(overview_commands, "new_session", lambda a: stub)
        rc = cmd_overview(_ns(command="overview", as_json=False))
        assert rc == 0
        text = capsys.readouterr().out
        for label in ("identity", "notifications", "watch badge", "presence",
                      "feed head", "threads", "registry"):
            assert label in text

    def test_command_human_prints_component_error(self, monkeypatch, capsys,
                                                   feed_page1):
        stub = self._canned(
            feed_page1,
            {"CometNotificationsBadgeCountQuery": NotLoggedInError("expired")})
        monkeypatch.setattr(overview_commands, "new_session", lambda a: stub)
        rc = cmd_overview(_ns(command="overview", as_json=False))
        assert rc == 0
        text = capsys.readouterr().out
        assert "ERROR: NotLoggedInError: expired" in text
        assert "watch badge" in text  # the rest of the dashboard still shown
