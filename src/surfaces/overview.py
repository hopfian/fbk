"""OVERVIEW aggregate: one dashboard pass over cheap verified reads (docs/15).

One operator dashboard composing the already live-verified read-only
surfaces — the overview introduces NO new wire shapes of its own.

ARCHITECTURE:

  Per-component failure isolation is the module's core invariant: every
  dashboard leg runs through ``_guarded``, which converts ANY exception —
  typed GraphQL errors, registry misses, malformed payloads — into an
  ``{"error": "<type>: <msg>"}`` value under that component's key. One
  broken surface therefore never blanks the dashboard; the operator still
  sees the other six reads plus a precise failure record for the seventh
  (a dashboard's value is that it degrades legibly, not atomically).
  Components construct their sibling services ad hoc from the shared
  session — no cached service state survives between ``collect()`` passes.

CALIBRATION NOTES:

  * Component provenance (docs/15): identity — the bootstrap view (no
    extra network call); notifications — CometNotificationsBadgeCountQuery
    unseen count; watch — useCometWatchBadgeCountQuery unseen-video badge;
    presence — chat-visibility status (online/offline); feed head — first
    page read, first story heads only; threads — messenger thread list
    length; registry — harvested doc_id registry size.
  * Every component leg is a read that was already live-verified in its
    own surface's calibration (see the respective surface modules); the
    aggregate is deliberately assembled from the cheapest verified reads
    only (docs/10 §2: reads have the high ceilings).

USER-DOC ANCHOR: cli/docs/04-reference-feed.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .base import Surface
from .feed import FeedService
from .messenger import MessengerService
from .notifications import NotificationsService
from .presence import PresenceService
from .video import VideoService

#: How many feed story heads the dashboard shows.
_FEED_HEAD_COUNT = 3
#: How many messenger threads are listed for the count read.
_THREAD_SAMPLE = 8
#: How much of a story's text rides a head entry.
_HEAD_TEXT_LEN = 80

#: The stable key set every collect() result carries, in dashboard order.
OVERVIEW_KEYS = (
    "identity",
    "notifications_badge",
    "watch_badge",
    "presence",
    "feed_head",
    "thread_count",
    "registry_size",
)


class OverviewService(Surface):
    """The aggregate dashboard: cheap verified reads, guarded per component."""

    def collect(self) -> dict[str, Any]:
        """One dashboard pass; every key of OVERVIEW_KEYS is always present.

        A component that raises (typed GraphQL error, registry miss, ...)
        is reported as ``{"error": "<type>: <msg>"}`` under its key; the
        remaining components are still collected.

        Returns:
            A dict keyed exactly by ``OVERVIEW_KEYS`` (dashboard order):
            identity fields, badge counts, presence, the feed head sample,
            the thread-count sample and the registry size — each value
            either the component's own shape or its ``{"error": ...}``
            record; never a partial-key dict.

        Raises:
            Nothing from the components — every leg is exception-guarded;
            only session-construction errors could surface.
        """
        return {
            "identity": self._guarded(self._identity),
            "notifications_badge": self._guarded(self._notifications_badge),
            "watch_badge": self._guarded(self._watch_badge),
            "presence": self._guarded(self._presence),
            "feed_head": self._guarded(self._feed_head),
            "thread_count": self._guarded(self._thread_count),
            "registry_size": self._guarded(lambda: len(self.registry)),
        }

    # ------------------------------------------------------------------ guard
    @staticmethod
    def _guarded(component: Callable[[], Any]) -> Any:
        """Run one dashboard component; a failure becomes {"error": ...}."""
        try:
            return component()
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}

    # -------------------------------------------------------------- components
    def _identity(self) -> dict[str, Any]:
        """The bootstrap identity — no extra call beyond the page fetch."""
        boot = self.session.bootstrap()
        return {
            "user_id": boot.user_id,
            "user_name": boot.user_name,
            "state": boot.state.value,
            "revision": boot.revision,
        }

    def _notifications_badge(self) -> int:
        """Unseen notifications count (live-verified badge query, docs/15)."""
        return NotificationsService(self.session).badge().unseen

    def _watch_badge(self) -> int:
        """Unseen-video count on the Watch top-tab bookmark."""
        return VideoService(self.session).badge()

    def _presence(self) -> dict[str, Any]:
        """The viewer's chat-visibility status."""
        return PresenceService(self.session).status()

    def _feed_head(self) -> dict[str, Any]:
        """First feed page: total count plus the first story heads only."""
        page = FeedService(self.session).read()
        heads: list[dict[str, Any]] = []
        for story in page.stories[:_FEED_HEAD_COUNT]:
            heads.append({
                "actor": story.actor.name if story.actor else None,
                "text": (story.text or "")[:_HEAD_TEXT_LEN] or None,
            })
        return {
            "count": len(page.stories),
            "heads": heads,
            "has_next_page": page.has_next_page,
        }

    def _thread_count(self) -> int:
        """How many recent messenger threads the list read returns."""
        return len(MessengerService(self.session).threads(limit=_THREAD_SAMPLE))
