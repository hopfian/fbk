"""Memories surface service (docs/02 §2 endpoint families, docs/15 §P2-2).

The throwback ("On This Day") feed read: one paginated feed query whose
live-gated variable template passed the 1675012 required-variable barrier.

ARCHITECTURE:

  The /memories/ page's own root query is NOT replayable (a verbatim
  replay is rejected server-side with ``field_type_no_match``), so the
  feed is served through the paginated pair documented below: one
  registry-backed ``CometMemoriesFeedQuery`` call per page, typed into
  ``MemoryCard`` rows plus ``MemoriesFeed`` pagination plumbing. The
  empty-throwback account state — a live-observed protocol-level
  rejection, not a failure — degrades to an empty feed inside the service
  so callers never see the account-state artifact.

CALIBRATION NOTES:

  Ground truth (live-probed 2026-09-18, read-only):

  * The /memories/ page SSR-registers ``CometMemoriesRootQuery``
    (39478874858377675, delta-harvested) — its embedded response lives in
    the page HTML (``viewer.throwback.throwback_units``), and a verbatim
    replay is rejected server-side (field_type_no_match), so the feed is
    served through the paginated pair:
  * FEED — ``CometMemoriesFeedQuery`` (doc_id 29291588250431417, registry).
    LocalArguments schema-decoded from the owning bundle Tx1fakQ4oig.js:
    count(10), cursor, feedLocation, feedbackSource, focusCommentID,
    goodwillTimestamp, privacySelectorRenderLocation,
    referringStoryRenderLocation, renderLocation, scale, useDefaultActor,
    plus the feed-story relay-provider flags. The operation is
    ``viewer { throwback(throwback_permalink_source_story_id: 0) {
    throwback_units(first: $count, after: $cursor) { edges { node }
    page_info } } }`` with unit nodes of concrete type Story /
    GoodwillCometStory / PaginatedPeopleYouMayKnowFeedUnit / ShowcaseFeedUnit
    / FriendRequestsFeedUnit / QuickPromotionNativeTemplateFeedUnit …
  * Live-variable gate (calibrated on the read-only replay): without the
    relay-provider set the server answers 1675012
    missing_required_variable_value; with it (the verbatim provider values
    the /memories/ page itself used) the request is accepted — the probe
    account then gets field_type_no_match because its throwback is EMPTY
    (the live SSR payload says "You've completely caught up",
    throwback_units: []). The template below is therefore the live-gated
    variable set, and the parser is decoded from the bundle operation plus
    the live SSR response shape.
  * ``CometMemoriesSetThrowbackSettingsMutation`` (9494473390636819,
    registry): LocalArgument ``input`` ->
    ``throwback_settings_edit(data:)`` is schema-decoded, but the inner
    input shape (ThrowbackSettingsEditInput) is NOT observable in any
    served     bundle — no settings command is exposed (honest omission).

USER-DOC ANCHOR: cli/docs/04-reference-feed.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import copy
import json
from typing import Any

from pydantic import BaseModel, Field

from graphql.errors import GraphQLProtocolError

from .base import Surface

FEED_QUERY = "CometMemoriesFeedQuery"
FEED_DOC_ID = "29291588250431417"

#: Live-probed 2026-09-18: the /memories/ page's own variable values for
#: the throwback context (feedLocation / feedbackSource / renderLocation /
#: scale / useDefaultActor and the full relay-provider set the SSR
#: registration carried), merged with the schema-decoded FeedQuery
#: argument set (count 10, cursor, goodwillTimestamp,
#: referringStoryRenderLocation — decoded defaults null). This exact set
#: passed the live 1675012 required-variable gate.
DEFAULT_MEMORIES_FEED_VARIABLES: dict[str, Any] = {
    "count": 10,
    "cursor": None,
    "feedLocation": "GOODWILL_THROWBACK_PERMALINK",
    "feedbackSource": 0,
    "focusCommentID": None,
    "goodwillTimestamp": None,
    "privacySelectorRenderLocation": "COMET_STREAM",
    "referringStoryRenderLocation": None,
    "renderLocation": "throwback_composer",
    "scale": 2,
    "useDefaultActor": False,
    "__relay_internal__pv__GHLShouldChangeAdIdFieldNamerelayprovider": True,
    "__relay_internal__pv__GHLShouldChangeSponsoredDataFieldNamerelayprovider": True,
    "__relay_internal__pv__CometFeedStory_enable_reactor_facepilerelayprovider": False,
    "__relay_internal__pv__CometFeedStory_enable_social_bubblesrelayprovider": False,
    "__relay_internal__pv__CometFeedStory_enable_post_permalink_white_space_clickrelayprovider": False,  # noqa: E501 (live-captured relay param key — unwrappable)
    "__relay_internal__pv__CometUFICommentActionLinksRewriteEnabledrelayprovider": True,
    "__relay_internal__pv__CometUFICommentAvatarStickerAnimatedImagerelayprovider": False,
    "__relay_internal__pv__IsWorkUserrelayprovider": False,
    "__relay_internal__pv__TestPilotShouldIncludeDemoAdUseCaserelayprovider": False,
    "__relay_internal__pv__FBReels_deprecate_short_form_video_context_gkrelayprovider": True,
    "__relay_internal__pv__FBReels_enable_view_dubbed_audio_type_gkrelayprovider": True,
    "__relay_internal__pv__CometFeedShareMedia_shouldPrefetchShareImagerelayprovider": False,
    "__relay_internal__pv__CometImmersivePhotoCanUserDisable3DMotionrelayprovider": False,
    "__relay_internal__pv__WorkCometIsEmployeeGKProviderrelayprovider": False,
    "__relay_internal__pv__IsMergQAPollsrelayprovider": False,
    "__relay_internal__pv__FBReelsMediaFooter_comet_enable_reels_ads_gkrelayprovider": True,
    "__relay_internal__pv__CometUFIReactionsEnableShortNamerelayprovider": False,
    "__relay_internal__pv__CometUFICommentAutoTranslationTyperelayprovider": "AUTO_TRANSLATE",
    "__relay_internal__pv__CometUFIShareActionMigrationrelayprovider": True,
    "__relay_internal__pv__CometUFISingleLineUFIrelayprovider": True,
    "__relay_internal__pv__relay_provider_comet_ufi_ssr_seo_deferrelayprovider": True,
    "__relay_internal__pv__CometUFI_dedicated_comment_routable_dialog_gkrelayprovider": True,
    "__relay_internal__pv__ReelsIFUCard_reelsIFULikeCountrelayprovider": False,
    "__relay_internal__pv__FBReelsIFUTileContent_reelsIFUPlayOnHoverrelayprovider": True,
    "__relay_internal__pv__GroupsCometGYSJFeedItemHeightrelayprovider": 206,
    "__relay_internal__pv__StoriesShouldEnablePhotosensitiveContentWarningrelayprovider": False,
    "__relay_internal__pv__ShouldEnableBakedInTextStoriesrelayprovider": False,
    "__relay_internal__pv__StoriesShouldIncludeFbNotesrelayprovider": True,
}


class MemoryCard(BaseModel):
    """One memories-feed unit (throwback_units edge node, bundle-decoded).

    Represents one unit of the paginated throwback feed — concrete Story /
    GoodwillCometStory / … types per the CALIBRATION NOTES taxonomy.
    ``feedback_id`` is the unit's UFI feedback head (b64 ``feedback:<id>``,
    docs/15 §P2-2) so follow-up reactions can address the card without a
    second read; ``text``/``date_text`` carry the story head the UI renders.
    """

    id: str | None = None
    typename: str | None = None
    text: str | None = None
    creation_time: int | None = None
    feedback_id: str | None = None
    date_text: str | None = None


class MemoriesFeed(BaseModel):
    """The memories feed page: typed cards + pagination plumbing.

    The full typed page for one ``CometMemoriesFeedQuery`` replay: the
    card rows, the Relay connection cursor pair (``end_cursor`` /
    ``has_next_page``, docs/04 §5 pagination convention), and a raw-payload
    size fingerprint for journal-side diffing of response scale.
    """

    cards: list[dict[str, Any]] = Field(default_factory=list)
    end_cursor: str | None = None
    has_next_page: bool = False
    raw_size: int = 0


def _card_text(node: dict[str, Any]) -> str | None:
    """First story text in the unit (Story units carry message.text)."""
    message = node.get("message")
    if isinstance(message, dict):
        text = message.get("text")
        if isinstance(text, dict):
            inner = text.get("text")
            if isinstance(inner, str):
                return inner
        if isinstance(text, str):
            return text
    direct = node.get("text")
    if isinstance(direct, str):
        return direct
    return None


def _card(node: dict[str, Any]) -> MemoryCard:
    """One throwback_units edge node -> a typed MemoryCard."""
    feedback = node.get("feedback")
    fid = feedback.get("id") if isinstance(feedback, dict) else None
    return MemoryCard(
        id=str(node["id"]) if node.get("id") else None,
        typename=(node.get("__typename")
                  if isinstance(node.get("__typename"), str) else None),
        text=_card_text(node),
        creation_time=(node.get("creation_time")
                       if isinstance(node.get("creation_time"), int) else None),
        feedback_id=str(fid) if fid else None,
        date_text=(node.get("date_text")
                   if isinstance(node.get("date_text"), str) else None),
    )


def _throwback_units(payload: dict[str, Any]) -> dict[str, Any] | None:
    viewer = payload.get("data", {}).get("viewer", {})
    throwback = viewer.get("throwback") if isinstance(viewer, dict) else None
    units = throwback.get("throwback_units") if isinstance(throwback, dict) else None
    return units if isinstance(units, dict) else None


def parse_memories_cards(payload: dict[str, Any]) -> list[MemoryCard]:
    """Merged memories payload -> typed MemoryCards (never raises on an
    empty feed; decoded path ``viewer.throwback.throwback_units.edges``).

    Args:
        payload: The merged GraphQL response from the feed-query replay.

    Returns:
        One MemoryCard per well-shaped edge node, document order; ``[]``
        when the payload carries no ``throwback_units.edges`` list.
    """
    out: list[MemoryCard] = []
    units = _throwback_units(payload)
    edges = units.get("edges") if isinstance(units, dict) else None
    if not isinstance(edges, list):
        return out
    for edge in edges:
        if isinstance(edge, dict) and isinstance(edge.get("node"), dict):
            out.append(_card(edge["node"]))
    return out


def parse_memories_feed(payload: dict[str, Any]) -> MemoriesFeed:
    """Full typed page: cards + the connection page_info (docs/04 §5).

    Args:
        payload: The merged GraphQL response from the feed-query replay.

    Returns:
        A ``MemoriesFeed`` — the typed card rows, the connection cursor
        pair, and the raw JSON size (a response-scale fingerprint).
    """
    units = _throwback_units(payload)
    page_info = units.get("page_info") if isinstance(units, dict) else None
    page_info = page_info if isinstance(page_info, dict) else {}
    return MemoriesFeed(
        cards=[c.model_dump() for c in parse_memories_cards(payload)],
        end_cursor=page_info.get("end_cursor") or None,
        has_next_page=bool(page_info.get("has_next_page")),
        raw_size=len(json.dumps(payload, default=str)),
    )


class MemoriesService(Surface):
    """Memories surface: the throwback feed read (docs/02 §2)."""

    def feed(self, *, limit: int = 10) -> list[dict[str, Any]]:
        """The memories feed, up to ``limit`` typed memory cards.

        Replays the live-gated CometMemoriesFeedQuery template with
        count=limit; cards are the throwback_units nodes (story text /
        date heads when the variant carries them, plus the story feedback
        head for follow-up UFI calls).

        Empty-throwback accounts (live-observed 2026-09-18): the server
        answers with a ``field_type_no_match`` protocol error — an empty
        throwback has no edge type to match — which maps to an empty
        feed. Every other protocol error still raises.

        Args:
            limit: Card cap; rides the wire as the ``count`` variable.

        Returns:
            ``MemoryCard.model_dump()`` dicts, document order; ``[]`` for
            an empty throwback (the account-state artifact) and for a
            genuinely empty feed alike.

        Raises:
            GraphQLProtocolError: Every protocol error EXCEPT the live-
                observed ``field_type_no_match`` empty-throwback artifact
                propagates unchanged.
            FBGraphError: Typed session/checkpoint/rate-limit errors
                propagate unchanged.
        """
        variables = copy.deepcopy(DEFAULT_MEMORIES_FEED_VARIABLES)
        variables["count"] = limit
        try:
            payload = self.client.call(FEED_QUERY,
                                       self.doc_id(FEED_QUERY), variables)
        except GraphQLProtocolError as exc:
            if "field_type_no_match" not in str(exc):
                raise
            return []
        page = parse_memories_feed(payload)
        return page.cards[:limit]
