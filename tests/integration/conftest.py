"""Live-integration fixtures: one REAL session per test module.

The suite is gated by FBK_LIVE=1 — the parent tests/conftest.py already
skips every ``@pytest.mark.live`` item without it, and this module mirrors
that gate (pytest_collection_modifyitems + a fixture-level skip) so the
integration dir stays inert even when collected with an unusual rootdir.

Everything here is read-only against the account except the single
state-neutral like/unlike lifecycle in test_live.py; the session journal
("live_integration") records every wire exchange for post-run review.

Human pacing (docs/11 §8): live tests run back-to-back otherwise, and ~30
unpaced persisted-query replays in under a minute is a textbook burst
signature the risk engine acts on (docs/10 §4 — live-observed 2026-09: an
unpaced full-suite run drew ``field_exception`` rejections mid-run and the
jar came back logged_out). The autouse ``human_pacing`` fixture sleeps a
lognormal inter-arrival gap before every live item, drawn per test id with
the project's own ``lognormal_schedule`` math — flurries and pauses, never
a metronome.
"""
from __future__ import annotations

import os
import time
import zlib
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:  # pragma: no cover - annotation-only import
    from session import Session

#: The public post used for the read-only comments test — a news-page
#: permalink whose comment tree survives the operator's own account state
#: (live-verified surface, docs/15 §P2-2). Synthetic in-tree; operators
#: point FBK_LIVE_PUBLIC_PERMALINK at a real public post permalink before
#: running the live suite.
PUBLIC_PERMALINK: str = os.environ.get(
    "FBK_LIVE_PUBLIC_PERMALINK",
    "https://www.facebook.com/sample.page/posts/"
    "pfbid02SampleSyntheticPostToken0123456789abcdef",
)

#: A public group id used for the read-only group-feed test — synthetic
#: in-tree; operators point FBK_LIVE_GROUP_ID at a real readable group
#: before running the live suite.
GROUP_ID: str = os.environ.get("FBK_LIVE_GROUP_ID", "12345678901234567")

#: Inter-arrival pacing between live tests (docs/11 §8): mean gap with the
#: default cv=0.6 lognormal spread, per-test deterministic seed, single-draw
#: tail capped so the whole suite stays inside its ~2.5-minute budget.
PACING_MEAN_GAP_S = 2.5
PACING_MAX_GAP_S = 8.0


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Second gate mirroring tests/conftest.py: skip every live-marked item
    unless FBK_LIVE=1 (kept local so the integration dir is safe even
    when the parent conftest is not in the collection path)."""
    if os.environ.get("FBK_LIVE") == "1":
        return
    skip = pytest.mark.skip(reason="live tests gated by FBK_LIVE=1")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def human_pacing(request: pytest.FixtureRequest) -> None:
    """Sleep a lognormal inter-arrival gap before every live test.

    Inert offline (no FBK_LIVE, no live marker → no sleep). The gap is
    drawn from the measurement surface's own lognormal math with a seed
    derived from the test id, so pacing is human-shaped yet reproducible;
    the tail is capped to keep the suite inside its runtime budget.
    """
    if os.environ.get("FBK_LIVE") != "1":
        return
    if "live" not in request.keywords:
        return
    from surfaces.measurement import lognormal_schedule

    seed = zlib.crc32(request.node.nodeid.encode("utf-8"))
    gap = lognormal_schedule(1, PACING_MEAN_GAP_S, seed=seed)[0]
    time.sleep(min(gap, PACING_MAX_GAP_S))


@pytest.fixture(scope="module")
def session() -> Session:
    """One real session per module (the bootstrap is the slow part).

    Builds a Session from the discovered Config (cookies.txt at the project
    root), bootstraps the homepage once, and hard-fails unless the state
    machine lands on logged_in — a checkpointed or logged-out jar cannot
    exercise any surface meaningfully. The journal is "live_integration" so
    live traffic stays separated from offline/operative journals.
    """
    if os.environ.get("FBK_LIVE") != "1":
        pytest.skip("live tests gated by FBK_LIVE=1")
    from config import Config
    from session import Session

    s = Session(Config.discover(), journal_name="live_integration")
    assert (
        s.bootstrap().state.value == "logged_in"
    ), "live suite requires a logged-in cookie jar"
    return s


@pytest.fixture(scope="module")
def public_permalink() -> str:
    """The public sample-post permalink used by the comments read test
    (operators override via FBK_LIVE_PUBLIC_PERMALINK)."""
    return PUBLIC_PERMALINK


@pytest.fixture(scope="module")
def group_id() -> str:
    """The public group id used by the group-feed read test (operators
    override via FBK_LIVE_GROUP_ID)."""
    return GROUP_ID
