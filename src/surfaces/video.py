"""VIDEO/WATCH surface service: the /watch/ feed and the unseen-video badge
(docs/02 §2.6 media family, docs/04 §5 pagination + §6 UFI, docs/15).

ARCHITECTURE:

  Feed reads follow the any-surface-page law (docs/15 §P3-5): GET
  /watch/, harvest the SSR preload registration (the unified player's
  entry query churns names across revisions, so the page preload is the
  doc_id source of truth), replay it verbatim. Chaining pagination
  replays the schema-decoded watch-and-scroll hook with the opaque
  chainingCursor echoed back. The badge query is the rare zero-argument
  persisted query (all args are query-text literals), so its replay
  variables are ``{}``. All traffic flows through Session/GraphQLClient,
  so the service is fully offline-testable against StubSession
  (tests/fakes.py).

CALIBRATION NOTES — wire ground truth, live-probed 2026-09:

* FEED: GET /watch/ preloads the unified video feed entry query
  (``FBUnifiedVideoRootWithEntrypointQuery``, registry doc_id
  38063999043248030) with verbatim variables — count, scale and the
  ``video_feed_context_data`` block (referral_source "fb_shorts_tab",
  video_channel_entry_point "VIDEOS_TAB", TAB surface type). Replaying it
  yields the seed video row; the merged response wraps the primary Video
  inside a feed-story node (no __typename — identified by its
  message/feedback shape) that carries the post caption
  (``message.text``) and UFI ``feedback.id`` the Video node itself
  omits, while Video nodes carry the numeric id, ``permalink_url`` (the
  watch_url / reel link), ``owner``, ``length_in_second`` and — on the
  shorts-scrubber variants — a top-level ``track_title``.
* CHAINING: next-video pagination rides ``CometWatchAndScrollChainingQuery``
  (registry doc_id 28164369823234560) — schema-decoded from its owning
  bundle (rsrc.php/v4il_D4/…/WIBAV8pFfQN.js): LocalArguments
  ``{caller, chainingCursor, channelEntryPoint, count, scale, seedVideoID}``
  and the watch-and-scroll hook commits ``{caller: "WNS",
  channelEntryPoint: "WNS", count: N, scale: WebPixelRatio, seedVideoID}``
  (live-verified replay). The sibling ``CometWatchAndScrollVideoQuery``
  (28867491862843621) takes ``{chainingCursor, chainingDisabled,
  chainingSeedVideoID, scale, videoID}`` for a single-video entry.
* BADGE: ``useCometWatchBadgeCountQuery`` (registry doc_id
  23979318198368825) — the decoded operation has NO LocalArguments (all
  args are literals: the Watch top-tab bookmark id "2392950137",
  environment COMET, folder PRODUCT), so the replay variables are ``{}``;
  the count rides ``viewer.bookmarks.edges[].node.unread_count``.
  Live-verified: the response carries exactly one bookmark edge.

All traffic flows through Session/GraphQLClient, so the service is fully
offline-testable against StubSession (tests/fakes.py).

USER-DOC ANCHOR: cli/docs/05-reference-content.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import copy
import json
import re
from typing import Any

from auth.bootstrap import extract_preload_registry

from .base import Surface, _walk_preorder

# --------------------------------------------------------------------- names
WATCH_URL = "https://www.facebook.com/watch/"

#: Any video-feed entry query the /watch/ page preloads counts (the unified
#: player entry point changed names across revisions; the page preload is
#: the source of truth, docs/15 §P2-2).
FEED_ENTRY_RE = re.compile(r"(?:FBUnifiedVideo|CometWatchAndScroll|Watch)\w*Query")
FALLBACK_FEED_QUERY = "FBUnifiedVideoRootWithEntrypointQuery"

CHAINING_QUERY = "CometWatchAndScrollChainingQuery"
VIDEO_QUERY = "CometWatchAndScrollVideoQuery"
BADGE_QUERY = "useCometWatchBadgeCountQuery"

#: The Watch top-tab bookmark id, decoded from the badge query's literal
#: arguments (bundle rsrc.php/v4/yK/r/R7O2FOlQfy_.js).
WATCH_BOOKMARK_ID = "2392950137"

#: Schema-decoded chaining-hook variables (live-verified replay): the
#: watch-and-scroll flow commits caller/channelEntryPoint "WNS" with the
#: opaque chainingCursor from the previous page and the seed video id.
DEFAULT_CHAINING_VARIABLES: dict[str, Any] = {
    "caller": "WNS",
    "chainingCursor": None,
    "channelEntryPoint": "WNS",
    "count": 1,
    "scale": 2,
    "seedVideoID": None,
}

#: Live-captured /watch/ preload variables (docs/15 §P2-2), used verbatim
#: when the fetched page carries no video preload (e.g. a stubbed session).
DEFAULT_WATCH_VARIABLES: dict[str, Any] = {
    "count": 1,
    "initial_node_id": "",
    "isAggregationProfileViewerOrShouldShowReelsForPage": False,
    "page_id": "",
    "root_video_id": "",
    "scale": 2,
    "should_include_comet_reels_seo_llm_content": False,
    "should_use_stream": True,
    "shouldIncludeInitialNodeFetch": False,
    "shouldShowReelsForPage": False,
    "shouldShowReelsForUser": False,
    "stream_initial_count": 1,
    "useDefaultActor": False,
    "user_id": "",
    "video_feed_context_data": {
        "arltw_feed_section_type": "FB_SHORTS_CHAINING",
        "in_session_watched_video": None,
        "is_async_ads_enabled": True,
        "is_async_ads_headload_coupling_enabled": True,
        "player_behavior": "UNIFIED_PLAYER_VDD",
        "real_time_ranking_context_data": {"recent_vpvs_v2": []},
        "referral_source": "fb_shorts_tab",
        "request_type": "NORMAL",
        "seed_video_id": None,
        "shorts_search_params": {},
        "surface_type": "TAB",
        "tracking_code": "",
        "video_channel_entry_point": "VIDEOS_TAB",
        "client_viewer_session_id": "9bcefbae-d39b-475b-b952-83bcbccb9c28",
    },
}

# Pagination plumbing rides at arbitrary depths in the merged payload
# (docs/04 §5), so extraction is a regex over the JSON re-serialisation.
_RE_END_CURSOR = re.compile(r'"end_cursor"\s*:\s*"([^"]+)"')
_RE_HAS_NEXT = re.compile(r'"has_next_page"\s*:\s*true')


def _video_row(node: dict[str, Any],
               story: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """One ``Video`` node -> a typed row, or None when shapeless.

    Title precedence (live-probed 2026-09-18 on the unified player feed):
    the node's own ``message.text`` / ``creation_story.message.text`` /
    ``pmv_metadata.track_title`` / top-level ``track_title`` (the
    shorts-scrubber nodes carry it there), else the message.text of the
    enclosing feed-story wrapper (``story``) — the response wraps the
    primary Video inside a story node that carries the post caption the
    Video node itself omits. ``feedback_id`` rides from the node's own
    feedback else the enclosing story's (the UFI target for follow-ups).
    """
    vid = node.get("id")
    if not vid:
        return None
    url = node.get("permalink_url") or node.get("shareable_url") or \
        node.get("url")
    owner = node.get("owner")
    title: str | None = _message_text(node.get("message"))
    if title is None:
        creation = node.get("creation_story")
        if isinstance(creation, dict):
            title = _message_text(creation.get("message"))
    if title is None:
        pmv = node.get("pmv_metadata")
        if isinstance(pmv, dict) and isinstance(pmv.get("track_title"), str):
            title = pmv["track_title"]
    if title is None and isinstance(node.get("track_title"), str):
        title = node["track_title"]
    feedback_id: str | None = None
    feedback = node.get("feedback")
    if not (isinstance(feedback, dict) and feedback.get("id")):
        feedback = story.get("feedback") if story is not None else None
    if isinstance(feedback, dict) and feedback.get("id"):
        feedback_id = str(feedback["id"])
    if title is None and story is not None:
        title = _message_text(story.get("message"))
    view_count: int | None = None
    for key in ("video_view_count", "view_count", "video_view_count_renderer"):
        val = node.get(key)
        if isinstance(val, bool):
            continue
        if isinstance(val, int):
            view_count = val
            break
        if isinstance(val, dict) and isinstance(val.get("count"), int):
            view_count = val["count"]
            break
    return {
        "id": str(vid),
        "url": url if isinstance(url, str) else None,
        "title": (title.splitlines()[0][:120] if title else None),
        "owner_id": (str(owner.get("id"))
                     if isinstance(owner, dict) and owner.get("id") else None),
        "owner_name": (str(owner.get("name"))
                       if isinstance(owner, dict) and owner.get("name")
                       else None),
        "length_s": (node["length_in_second"]
                     if isinstance(node.get("length_in_second"),
                                   (int, float)) else None),
        "view_count": view_count,
        "feedback_id": feedback_id,
    }


def _message_text(message: Any) -> str | None:
    """A ``message`` dict's text (plain string or {"text": str} variant)."""
    if isinstance(message, dict):
        text = message.get("text")
        if isinstance(text, dict):
            inner = text.get("text")
            if isinstance(inner, str):
                return inner
        if isinstance(text, str):
            return text
    return None


def _is_story_shape(node: dict[str, Any]) -> bool:
    """A feed-story-shaped dict: carries a message with text or a feedback
    id (live-observed: the unified player's wrapping story node has NO
    __typename, so identification is by shape, not type name)."""
    if _message_text(node.get("message")) is not None:
        return True
    feedback = node.get("feedback")
    return isinstance(feedback, dict) and bool(feedback.get("id"))


def parse_video_feed(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Merged video payload -> typed rows, deduped on the video id.

    The walk threads the nearest story-shaped ancestor (any dict carrying
    message/feedback — live-probed 2026-09-18) into each Video row so the
    post caption and UFI feedback id survive even when the Video node
    itself carries neither; duplicate Video ids (the player repeats nodes)
    keep the first occurrence in document order.

    Args:
        payload: The merged video-feed response document (streamed
            chunks already combined).

    Returns:
        Typed video row dicts (id, url, title head, owner, length,
        view_count, feedback_id) in document order, deduped on id.
    """
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    stack: list[tuple[Any, dict[str, Any] | None]] = [(payload, None)]
    while stack:
        cur, story = stack.pop()
        if isinstance(cur, dict):
            if _is_story_shape(cur):
                story = cur
            if cur.get("__typename") == "Video":
                row = _video_row(cur, story)
                if row is not None and row["id"] not in seen:
                    seen.add(row["id"])
                    rows.append(row)
            children: list[Any] = list(cur.values())
        elif isinstance(cur, list):
            children = list(cur)
        else:
            continue
        stack.extend(reversed([(child, story) for child in children]))
    return rows


class VideoService(Surface):
    """The video/watch surface: feed read, chaining, badge (docs/02 §2.6)."""

    # ------------------------------------------------------------------ feed
    def watch_feed(self, *, limit: int = 10,
                   cursor: str | None = None,
                   seed_video_id: str | None = None) -> dict[str, Any]:
        """One page of the /watch/ video feed (live pattern, read-only).

        Without a cursor: GETs /watch/, harvests the video-feed preload
        (verbatim variables, docs/15 §P2-2), sets ``count`` to ``limit``
        and replays it; the merged payload is walked for Video nodes.

        With a cursor: replays CometWatchAndScrollChainingQuery (the decoded
        watch-and-scroll chaining hook: caller/channelEntryPoint "WNS",
        count, scale, the opaque chainingCursor and the seedVideoID — the id
        of the last video of the previous page) for next-video pagination.

        Args:
            limit: Page size; rides the ``count`` variable and slices
                the typed rows locally so the wire shape is otherwise
                never edited.
            cursor: The opaque chainingCursor echoed back from the
                previous page's end_cursor; None selects the initial
                preload replay.
            seed_video_id: The chaining seed — the id of the last video
                of the previous page (ignored without a cursor).

        Returns:
            A dict: {videos, count, end_cursor, has_next_page, raw_size}
            — the videos are typed rows from parse_video_feed, and
            raw_size supports soft-block signature logging.
        """
        if cursor is None:
            html = self._fetch(WATCH_URL)
            entry = None
            for candidate in extract_preload_registry(html):
                if candidate.query_name and \
                        FEED_ENTRY_RE.fullmatch(candidate.query_name):
                    entry = candidate
                    break
            if entry is not None:
                query_name: str = entry.query_name or FALLBACK_FEED_QUERY
                doc_id: str = entry.doc_id
                variables: dict[str, Any] = dict(entry.variables)
            else:
                query_name = FALLBACK_FEED_QUERY
                doc_id = self.doc_id(FALLBACK_FEED_QUERY)
                variables = copy.deepcopy(DEFAULT_WATCH_VARIABLES)
            variables["count"] = limit
        else:
            query_name = CHAINING_QUERY
            doc_id = self.doc_id(CHAINING_QUERY)
            variables = {**copy.deepcopy(DEFAULT_CHAINING_VARIABLES),
                         "count": limit, "chainingCursor": cursor,
                         "seedVideoID": seed_video_id}
        data = self.client.call(query_name, doc_id, variables)
        videos = parse_video_feed(data)[:limit]
        raw = json.dumps(data, default=str)
        end = _RE_END_CURSOR.search(raw)
        return {
            "videos": videos,
            "count": len(videos),
            "end_cursor": end.group(1) if end else None,
            "has_next_page": _RE_HAS_NEXT.search(raw) is not None,
            "raw_size": len(raw),
        }

    # ----------------------------------------------------------------- badge
    def badge(self) -> int:
        """The unseen-video count for the Watch top-tab bookmark.

        useCometWatchBadgeCountQuery decodes with NO LocalArguments (every
        argument is a query-text literal — the bookmark id 2392950137,
        environment COMET), so the replay sends empty variables; the count
        rides viewer.bookmarks.edges[].node.unread_count (docs/15).

        Returns:
            The highest unread_count across the bookmark edges
            (live-verified: the response carries exactly one edge); 0
            when the payload carries no readable count.
        """
        data = self.client.call(BADGE_QUERY, self.doc_id(BADGE_QUERY), {})
        count = 0
        for node in _walk_preorder(data):
            edges = None
            bookmarks = node.get("bookmarks") if isinstance(
                node.get("bookmarks"), dict) else None
            if bookmarks is not None and isinstance(
                    bookmarks.get("edges"), list):
                edges = bookmarks["edges"]
            if edges is None:
                continue
            for edge in edges:
                if not isinstance(edge, dict):
                    continue
                item = edge.get("node")
                if not isinstance(item, dict):
                    continue
                unread = item.get("unread_count")
                if isinstance(unread, int) and not isinstance(unread, bool):
                    count = max(count, unread)
            return count
        return count
