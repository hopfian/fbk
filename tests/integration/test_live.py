"""LIVE integration smoke suite — every read-only surface + the one
state-neutral reaction lifecycle (FBK_LIVE=1 required).

Contract (docs/15 live-calibration methodology):

* Each test constructs its surface service over the module-scoped REAL
  session and asserts STRUCTURAL sanity only — the account's data state
  varies, so empty lists pass whenever the shape is right. These are
  integration smoke tests, not data assertions.
* Everything is read-only except ``test_react_unreact_lifecycle``: a
  LIKE applied and then removed on one of the operator's OWN feed stories
  (net-zero, live-verified in Phase 2). Publish/comment-create/friend/
  group/page/messenger/saved mutations are deliberately NOT exercised.
* No secrets in assertion messages: cookie values, full comment bodies
  and message texts never appear — only length-bounded heads.
"""
from __future__ import annotations

import base64
import json
import os

import pytest

from domain.common import (
    CommentID,
    FeedPage,
    Message,
    NotificationCount,
    Profile,
    ReactionType,
    SearchResponse,
    ThreadSummary,
    User,
)
from session import Session
from surfaces.comments import CommentsService
from surfaces.feed import FeedService
from surfaces.friends import FriendsService
from surfaces.groups import GroupsService
from surfaces.marketplace import MarketplaceService
from surfaces.measurement import LatencyReport, MeasurementService
from surfaces.memories import MemoriesService
from surfaces.messenger import MessengerService
from surfaces.notifications import NotificationsService
from surfaces.pages import PagesService
from surfaces.presence import PresenceService
from surfaces.profile import ProfileService, ProfileView
from surfaces.saved import SavedService
from surfaces.search import SearchService
from surfaces.stories import StoriesService
from surfaces.video import VideoService

# Live-gated smokes: require FBK_LIVE=1 plus a logged-in cli/cookies.txt and
# pace themselves via the integration conftest's human_pacing fixture.
# Assertions are structural only — empty result sets pass when the account's
# data state is empty. Nothing here mutates account state except
# test_react_unreact_lifecycle, which applies and immediately removes a single
# LIKE on the operator's own feed story (net zero).
pytestmark = pytest.mark.live

#: A public page vanity slug whose profile timeline is readable live —
#: synthetic in-tree; operators point FBK_LIVE_PAGE_VANITY at a real public
#: page before running the live suite.
PAGE_VANITY = os.environ.get("FBK_LIVE_PAGE_VANITY", "sample.page")

#: The E2EE person-to-person thread id used by the history-protocol and
#: send-guard tests (docs/15 §P7 — history maps the protocol rejection to
#: []; the send guard refuses plaintext sends to it before any HTTP).
#: Synthetic in-tree; operators point FBK_LIVE_E2EE_THREAD_ID at a real
#: E2EE thread before running the live suite.
E2EE_THREAD_ID = os.environ.get("FBK_LIVE_E2EE_THREAD_ID",
                                "12345678901234560")


def _head(text: str | None, size: int = 24) -> str:
    """A length-bounded head of a payload string, safe for assert messages."""
    if not text:
        return "<empty>"
    return text[:size]


# --------------------------------------------------------------------------- reads
def test_whoami_logged_in(session: Session) -> None:
    """Bootstrap identity: logged_in state, uid == c_user cookie, DTSG and
    revision present — the preconditions every other live test relies on."""
    boot = session.bootstrap()
    assert boot.state.value == "logged_in"
    assert boot.user_id == session.cookies.get("c_user"), (
        "bootstrap USER_ID must match the cookie jar's c_user"
    )
    assert boot.dtsg(), "live bootstrap must harvest a fb_dtsg token"
    assert boot.revision is not None and boot.revision.isdigit(), (
        "server revision must be a digit string (SiteData server_revision)"
    )


def test_feed_read(session: Session) -> None:
    """FeedService.read() returns a typed FeedPage with a substantial raw
    payload (the merged CometModernHomeFeedQuery response is ~900KB live)."""
    page = FeedService(session).read()
    assert isinstance(page, FeedPage)
    assert page.raw_size > 10000, (
        f"feed raw payload suspiciously small: {page.raw_size} bytes"
    )
    assert isinstance(page.has_next_page, bool)
    if page.stories:
        assert page.stories[0].id, "first parsed story should carry an id"


def test_feed_paginate(session: Session) -> None:
    """FeedService.paginate(end_cursor) returns another FeedPage when page 1
    exposed a cursor (skipped otherwise — cursor presence varies by serve)."""
    service = FeedService(session)
    first = service.read()
    if not first.end_cursor:
        pytest.skip("feed page 1 carried no end_cursor")
    second = service.paginate(first.end_cursor)
    assert isinstance(second, FeedPage)


def test_search(session: Session) -> None:
    """SearchService.search('python') returns at least one typed result
    carrying a name and a __typename (the registry-typed global search)."""
    response = SearchService(session).search("python")
    assert isinstance(response, SearchResponse)
    assert len(response.results) >= 1, "expected at least one search result"
    named = [r for r in response.results if r.name and r.typename]
    assert named, (
        "expected a search result with both a name and a __typename; got "
        f"{len(response.results)} results, first typename="
        f"{_head(response.results[0].typename)}"
    )


def test_notifications_badge(session: Session) -> None:
    """NotificationsService.badge() returns a typed NotificationCount with
    an integer unseen count (live-verified badge query)."""
    badge = NotificationsService(session).badge()
    assert isinstance(badge, NotificationCount)
    assert isinstance(badge.unseen, int)


def test_profile_me(session: Session) -> None:
    """ProfileService.me() returns the operator's own profile: same user id
    as the session, non-empty display name."""
    profile = ProfileService(session).me()
    assert isinstance(profile, Profile)
    assert profile.user.id == session.user_id()
    assert profile.user.name, "profile name should be non-empty"


def test_messenger_threads(session: Session) -> None:
    """MessengerService.threads() returns a list of typed thread summaries;
    the account may carry only a handful of threads (possibly zero)."""
    threads = MessengerService(session).threads()
    assert isinstance(threads, list)
    for thread in threads:
        assert isinstance(thread, ThreadSummary)
        assert thread.id, "any returned thread must carry an id"


def test_friends_list(session: Session) -> None:
    """FriendsService.list() returns a list of typed Users without raising;
    the friends set may legitimately be empty for a fresh account."""
    friends = FriendsService(session).list()
    assert isinstance(friends, list)
    for friend in friends:
        assert isinstance(friend, User)
        assert friend.id


def test_stories_tray(session: Session) -> None:
    """StoriesService.tray() returns a (possibly empty) list of tile dicts —
    structure only, tray contents vary with the social graph."""
    tiles = StoriesService(session).tray()
    assert isinstance(tiles, list)


def test_marketplace_browse(session: Session) -> None:
    """MarketplaceService.browse(limit=5) returns at most five listings;
    any returned listing carries an id and a name."""
    listings = MarketplaceService(session).browse(limit=5)
    assert isinstance(listings, list)
    assert len(listings) <= 5
    if listings:
        assert any(row.id and row.name for row in listings), (
            "marketplace listings should carry id+name; got "
            f"{len(listings)} rows, first id head={_head(listings[0].id)}"
        )


def test_saved_list(session: Session) -> None:
    """SavedService.list() returns the saved-items dashboard as a list of
    row dicts; a zero-row dashboard is a valid empty state."""
    rows = SavedService(session).list()
    assert isinstance(rows, list)
    for row in rows:
        assert isinstance(row, dict)
        assert row.get("id"), "saved rows must carry an id"


def test_comments_read(session: Session, public_permalink: str) -> None:
    """CommentsService.read() on the public sample-post permalink returns
    typed comments whose b64 ids decode to 'comment:…' (docs/04 §7 global
    ids)."""
    comments = CommentsService(session).read(public_permalink, limit=10)
    assert isinstance(comments, list)
    for comment in comments:
        raw = comment.id.raw
        decoded = base64.b64decode(raw + "=" * (-len(raw) % 4)).decode(
            "utf-8", "replace"
        )
        assert decoded.startswith("comment:"), (
            "comment id must decode to a 'comment:' global id; got head="
            f"{_head(decoded)}"
        )
        assert isinstance(comment.id, CommentID)


def test_video_feed_and_badge(session: Session) -> None:
    """VideoService.watch_feed(limit=3) runs and returns the structural
    dict ({videos, count, ...}); badge() returns an integer unseen count."""
    service = VideoService(session)
    feed = service.watch_feed(limit=3)
    assert isinstance(feed, dict)
    assert isinstance(feed["videos"], list)
    assert isinstance(feed["count"], int)
    badge = service.badge()
    assert isinstance(badge, int)


def test_groups_feed(session: Session, group_id: str) -> None:
    """GroupsService.feed_read() on the public group returns a parsed
    FeedPage (>= 0 stories — group content varies)."""
    page = GroupsService(session).feed_read(group_id)
    assert isinstance(page, FeedPage)
    assert isinstance(page.stories, list)
    assert page.raw_size > 0, "group feed payload should not be empty"


def test_presence(session: Session) -> None:
    """PresenceService.status() returns the viewer's chat-visibility dict
    (viewer.chat_visibility is a bool for a logged-in session)."""
    status = PresenceService(session).status()
    assert isinstance(status, dict)
    assert isinstance(status["chat_visibility"], bool)


def test_pages_feed(session: Session) -> None:
    """PagesService.feed_read(PAGE_VANITY) resolves the vanity slug via
    the page's own preloads and returns a parsed FeedPage."""
    page = PagesService(session).feed_read(PAGE_VANITY)
    assert isinstance(page, FeedPage)
    assert isinstance(page.stories, list)


def test_registry_loaded(session: Session) -> None:
    """The persisted-query registry is fully loaded (>1000 friendly->doc_id
    pairs from the harvested bundles, docs/13 §2)."""
    assert len(session.registry) > 1000, (
        f"registry too small: {len(session.registry)} pairs "
        f"(source={session.registry.source})"
    )


# ----------------------------------------------- the one allowed mutation lifecycle
def test_react_unreact_lifecycle(session: Session) -> None:
    """State-neutral LIKE -> REMOVE lifecycle on one of the operator's own
    feed stories (live-verified pair, docs/15 §P2-3): apply a LIKE, then
    remove it — net zero account state. Skips when the current feed serves
    no story with a feedback context."""
    service = FeedService(session)
    page = service.read()
    target = next(
        (story.feedback for story in page.stories if story.feedback), None
    )
    if target is None:
        pytest.skip("no story with a feedback id in the current feed")
    response = service.react(target.id, ReactionType.LIKE)
    payload = json.dumps(response, default=str)
    assert "reaction_count" in payload or not response.get("errors"), (
        "react response should carry reaction_count info or no errors key"
    )
    removal = service.unreact(target.id)
    assert not removal.get("errors"), (
        "unreact response should carry no errors key"
    )


# ------------------------------------------------------------- extended coverage
def test_profile_view_other(session: Session) -> None:
    """ProfileService.view() on the public sample page: the SSR preload
    resolves the vanity slug, the replay parses a FeedPage of the page's
    posts (>= 0 stories, each carrying an id/feedback or a permalink)."""
    view = ProfileService(session).view(PAGE_VANITY, limit=5)
    assert isinstance(view, ProfileView)
    assert isinstance(view.feed, FeedPage)
    assert isinstance(view.feed.stories, list)
    assert len(view.feed.stories) <= 5
    for story in view.feed.stories:
        assert story.id or story.key or story.feedback or story.permalink, (
            "every parsed profile story must carry an id, key, feedback "
            "context or permalink"
        )


def test_marketplace_search(session: Session) -> None:
    """MarketplaceService.search('laptop') replays the search page's own
    preload and returns at most ``limit`` listings, each with id + name
    (live-verified 2026-09: 22 listings for 'laptop')."""
    rows = MarketplaceService(session).search("laptop", limit=5)
    assert isinstance(rows, list)
    assert len(rows) <= 5
    for row in rows:
        assert row.id, "marketplace search rows must carry an id"
        assert row.name, (
            "marketplace search rows must carry a listing name; got id head="
            f"{_head(row.id)}"
        )


def test_marketplace_browse_and_search_distinct(session: Session) -> None:
    """Both marketplace entry points execute against the live session in
    the same test: browse() via the browse template and search() via the
    search page's verbatim preload — each capped at 3 rows."""
    service = MarketplaceService(session)
    browsed = service.browse(limit=3)
    searched = service.search("phone", limit=3)
    assert isinstance(browsed, list)
    assert isinstance(searched, list)
    assert len(browsed) <= 3
    assert len(searched) <= 3


def test_friends_suggestions(session: Session) -> None:
    """FriendsService.suggestions() reads the pymk_grid connection of the
    friends root query — structure only (this account may legitimately
    carry zero suggestion rows)."""
    suggestions = FriendsService(session).suggestions(limit=5)
    assert isinstance(suggestions, list)
    assert len(suggestions) <= 5
    for person in suggestions:
        assert isinstance(person, User)
        assert person.id, "suggestion rows must carry an id"


def test_memories_graceful_empty(session: Session) -> None:
    """MemoriesService.feed() maps the empty-throwback ``field_type_no_match``
    protocol rejection to [] (docs/15 live finding) — the call must return a
    list without raising on this account's empty throwback."""
    cards = MemoriesService(session).feed()
    assert isinstance(cards, list)


def test_messenger_history_protocol_rejection(session: Session) -> None:
    """MessengerService.history() on the E2EE thread returns a list without
    raising (docs/15 §P7: the plane answers a deterministic protocol
    rejection mapped to an empty history); any returned row is Message-typed."""
    rows = MessengerService(session).history(E2EE_THREAD_ID)
    assert isinstance(rows, list)
    for row in rows:
        assert isinstance(row, Message)


def test_messenger_send_guard_live(session: Session) -> None:
    """NEGATIVE, wire-safe: the E2EE send guard fires against the REAL
    session — send() to a non-self thread raises NonSelfThreadError BEFORE
    any HTTP is issued, so no message ever leaves."""
    with pytest.raises(MessengerService.NonSelfThreadError):
        MessengerService(session).send(E2EE_THREAD_ID, "should never go out")


def test_measure_latency_small(session: Session) -> None:
    """MeasurementService.latency(samples=3) runs a 3-probe canary window
    and returns a LatencyReport with every sample accounted for (ok+failed)
    and a positive mean over the successful probes."""
    report = MeasurementService(session).latency(samples=3)
    assert isinstance(report, LatencyReport)
    assert report.samples == 3
    assert report.ok + report.failed == 3
    assert report.mean_s > 0, (
        "a live canary window with any successful probe must carry a "
        f"positive mean; got ok={report.ok} failed={report.failed}"
    )


def test_saved_and_comments_service_shapes(
    session: Session, public_permalink: str
) -> None:
    """The saved dashboard and the public-permalink comments read still
    compose together: saved rows are id-carrying dicts, comment ids decode
    to 'comment:…' global ids (docs/04 §7)."""
    rows = SavedService(session).list()
    assert isinstance(rows, list)
    for row in rows:
        assert isinstance(row, dict)
        assert row.get("id"), "saved rows must carry an id"
    comments = CommentsService(session).read(public_permalink, limit=3)
    assert isinstance(comments, list)
    for comment in comments:
        raw = comment.id.raw
        decoded = base64.b64decode(raw + "=" * (-len(raw) % 4)).decode(
            "utf-8", "replace"
        )
        assert decoded.startswith("comment:"), (
            "comment id must decode to a 'comment:' global id; got head="
            f"{_head(decoded)}"
        )
        assert isinstance(comment.id, CommentID)


def test_video_feed_titles(session: Session) -> None:
    """VideoService.watch_feed() rows carry the title fix: any present title
    is a single-line text head (a non-numeric string capped at 120 chars),
    never a raw payload fragment."""
    feed = VideoService(session).watch_feed(limit=3)
    assert isinstance(feed, dict)
    assert isinstance(feed["videos"], list)
    for row in feed["videos"]:
        title = row.get("title")
        if title:
            assert isinstance(title, str), (
                f"video titles must be strings, got {type(title).__name__}"
            )
            assert not title.isdigit(), (
                "video titles must be text heads, not numeric values; got "
                f"{_head(title)}"
            )
            assert "\n" not in title, "video titles are single-line heads"
            assert len(title) <= 120


def test_upload_service_module_loads() -> None:
    """The upload surfaces import cleanly and expose their service classes —
    module/import shape only, no live upload is attempted."""
    from surfaces.upload import UploadService
    from surfaces.video_upload import VideoUploadService

    assert isinstance(UploadService, type)
    assert isinstance(VideoUploadService, type)


def test_group_feed_stories_deduped(session: Session, group_id: str) -> None:
    """GroupsService.feed_read() stories pass the stub/duplicate filter: no
    two retained stories share the same (key/id, feedback_id) identity with
    an all-None text/actor stub echo (the live-observed duplicate symptom)."""
    page = GroupsService(session).feed_read(group_id)
    assert isinstance(page, FeedPage)
    seen_pairs: set[tuple[str | None, str | None]] = set()
    for story in page.stories:
        key_id = story.key.raw if story.key else story.id
        if key_id is None:
            continue
        feedback_id = story.feedback.id.raw if story.feedback else None
        identity = (key_id, feedback_id)
        assert identity not in seen_pairs, (
            "group feed retained two stories with the same (key/id, "
            "feedback_id) identity — the stub/duplicate filter must have "
            f"dropped the echo (key head={_head(key_id)})"
        )
        seen_pairs.add(identity)
