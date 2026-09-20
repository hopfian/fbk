"""Pages surface service: page (additional-profile) creation, like, and
follow mutations over the GraphQL persisted-query plane (docs/02 §2.5 and
§2.10; docs/15 §3).

ARCHITECTURE:

  Timeline reads follow the any-surface-page law (docs/15 §P3-5): GET
  the page URL, harvest the SSR preload registration, replay it with the
  page id substituted as the top-level ``userID`` variable — the
  verbatim-replay invariant (docs/15 §P2-2). Mutations ship
  schema-decoded templates with a fresh minified-product attribution
  string (docs/15 §P2-3 wire shape) and client_mutation_id per call.
  All traffic flows through Session/GraphQLClient, so the service is
  fully offline-testable against StubSession (tests/fakes.py).

CALIBRATION NOTES:

  Every mutation template below is schema-decoded (2026-09) from the
  owning rsrc.php bundle of its mutation: the LocalArgument set comes
  from the ``__d("<Name>.graphql")`` module's ``argumentDefinitions``
  and the input object from the JS wrapper that constructs the commit
  variables (the docs/13 §2 bundle-diff methodology applied to the
  /pages/creation/ chunk set). These are MUTATIONS — the service is real
  and runnable, but live execution is the operator's call (docs/11 §2).

  Decoded argument schemas (bundle-verified, live 2026-09):
  * AdditionalProfilePlusCreationMutation  LocalArguments: input
    data: additional_profile_plus_create(data: $input) ->
    {additional_profile{id}, page{id}, name_error, error_message}
  * CometPageLikeCommitMutation            LocalArguments: input
    data: page_like(data: $input) -> {page{id, is_viewer_fan,
    subscribe_status}}
  * CometPageFollowCommitMutation          LocalArguments: input
    data: actor_subscribe(data: $input) -> subscribee

USER-DOC ANCHOR: cli/docs/06-reference-people.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import copy
import json
from typing import Any

from auth.bootstrap import extract_preload_registry
from domain.common import FeedPage, Story

from .base import Surface, _attribution, _fill, _mutation_id, _walk_preorder
from .feed import (
    _RE_END_CURSOR,
    _RE_HAS_NEXT,
    _extract_story,
)
from .groups import _dedupe_page_stories

CREATE_MUTATION = "AdditionalProfilePlusCreationMutation"
LIKE_MUTATION = "CometPageLikeCommitMutation"
FOLLOW_MUTATION = "CometPageFollowCommitMutation"

# The page-content feed query (live-probed 2026-09 on the public sample
# page, https://www.facebook.com/sample.page): the page URL preloads
# ProfileCometTimelineListViewRootQuery (doc_id 28117370721250101) with the
# VERBATIM variables the server itself used — replaying them is the single
# most faithful read (docs/15 §P2-2). The page's own numeric id rides as the
# top-level ``userID`` variable.
#
# Probe note: the same page also preloads ProfileCometTimelineFeedQuery
# (28236193016022113), but replaying that query (verbatim or modified)
# returns partial server errors at user.delegate_page.* field_exception /
# ctwa_ad4ad_insights — the list-view root query is the clean-replaying
# page content query, so it is the one this service uses.
PAGE_FEED_QUERY = "ProfileCometTimelineListViewRootQuery"
PAGE_FEED_DOC_ID = "28117370721250101"

# schema-decoded 2026-09: AdditionalProfilePlusCreateData as committed by the
# useAdditionalProfilePlusCreateMutation hook (creation_source "comet" is
# the decoded gate-20935 branch; page_referrer mirrors the route ref_type).
CREATE_VARIABLES: dict[str, Any] = {
    "input": {
        "bio": None,
        "categories": "{categories}",
        "client_mutation_id": "{client_mutation_id}",
        "creation_source": "comet",
        "name": "{name}",
        "off_platform_creator_reachout_id": None,
        "page_referrer": "branded_page",
    },
}

# schema-decoded 2026-09: PageLikeData as committed by the decoded
# pageLikeCommitMutationAction wrapper (source per the
# GraphQLFanFBPageActionOriginValueHackEnum; actor_id/client_mutation_id
# per the analogous live-captured shapes in docs/15 §P2-3).
LIKE_VARIABLES: dict[str, Any] = {
    "input": {
        "actor_id": "{actor_id}",
        "attribution_id_v2": "{attribution}",
        "client_mutation_id": "{client_mutation_id}",
        "is_tracking_encrypted": False,
        "page_id": "{page_id}",
        "source": "PAGE_TIMELINE",
        "tracking": None,
    },
}

# schema-decoded 2026-09: ActorSubscribeData as committed by the decoded
# pageFollowCommitMutationAction wrapper (subscribe_location default
# PAGE_PROFILE_HEADER, is_tracking_encrypted true).
FOLLOW_VARIABLES: dict[str, Any] = {
    "input": {
        "actor_id": "{actor_id}",
        "attribution_id_v2": "{attribution}",
        "client_mutation_id": "{client_mutation_id}",
        "is_tracking_encrypted": True,
        "secondary_subscribe_status": None,
        "subscribe_location": "PAGE_PROFILE_HEADER",
        "subscribee_id": "{page_id}",
        "tracking": None,
    },
}

# live-probed 2026-09: the full preloader variables for
# ProfileCometTimelineListViewRootQuery, harvested from the page SSR
# registration on /sample.page (docs/15 §P2-2). The page id appears ONLY as
# the top-level ``userID`` variable (probe: page id 12345678901234568,
# synthetic in-tree), so it is the one parameterized placeholder. Used
# whenever a page preload is not available (e.g. a stubbed session in tests).
DEFAULT_PAGE_FEED_VARIABLES: dict[str, Any] = {
    "previousProfileId": None,
    "privacySelectorRenderLocation": "COMET_STREAM",
    "renderLocation": "timeline",
    "scale": 2,
    "userID": "{page_id}",
    "__relay_internal__pv__GHLShouldChangeSponsoredDataFieldNamerelayprovider": True,
    "__relay_internal__pv__WorkCometIsEmployeeGKProviderrelayprovider": False,
    "__relay_internal__pv__GroupsCometGroupChatLazyLoadLastMessageSnippetrelayprovider": False,
    "__relay_internal__pv__ProfileCometFeaturedHighlightsPortraitAspectRatioGKrelayprovider": False,
    "__relay_internal__pv__WebPixelRatiorelayprovider": 2,
}


def _parse_page_feed(payload: dict[str, Any]) -> FeedPage:
    """Merged page-timeline payload -> FeedPage (docs/02 §2.5, docs/04 §5-§7).

    Page timelines carry their stories as ``HighlightPostUnit`` nodes (each
    wrapping the full story shape: feedback, comet_sections message, url)
    rather than bare ``Story`` __typename nodes — live-probed 2026-09 on the
    sample-page replay. Both node spellings are walked with the shared feed
    extractors (surfaces/feed.py); walked stories then pass the shared
    stub/duplicate filter from surfaces/groups.py (live-observed attachment
    sub-nodes echo an empty duplicate Story entry, docs/15 §P5-5);
    pagination plumbing is regex-extracted with the same live-calibrated
    patterns as the home feed.

    Args:
        payload: The merged ProfileCometTimelineListViewRootQuery response
            document (streamed chunks already combined).

    Returns:
        The typed FeedPage; HighlightPostUnit and bare Story nodes are
        normalized into the same typed rows.
    """
    stories: list[Story] = []
    for node in _walk_preorder(payload):
        if not isinstance(node, dict):
            continue
        typename = node.get("__typename")
        if typename == "Story" or (typename == "HighlightPostUnit" and node.get("type") == "POST"
              and node.get("id")):
            stories.append(_extract_story(node))
    stories = _dedupe_page_stories(stories)
    raw = json.dumps(payload, default=str)
    cursor = _RE_END_CURSOR.search(raw)
    return FeedPage(
        stories=stories,
        end_cursor=cursor.group(1) if cursor else None,
        has_next_page=_RE_HAS_NEXT.search(raw) is not None,
        raw_size=len(raw),
    )


class PagesService(Surface):
    """Page surface: timeline feed reads plus create/like/follow mutations."""

    def create(self, name: str, *, category: str | None = None) -> dict[str, Any]:
        """Create a new Page via the current additional-profile flow
        (docs/02 §2.5).

        Args:
            name: The Page name; rides input.name.
            category: Optional single category key seeding the decoded
                input's ``categories`` list (the decoded hook commits
                a list of keys, one per selected category).

        Returns:
            The merged ``additional_profile_plus_create`` response
            carrying the new ``page.id``.
        """
        categories = [category] if category else []
        variables = _fill(CREATE_VARIABLES, {
            "name": name,
            "categories": categories,
            "client_mutation_id": _mutation_id(),
        })
        return self.client.call(CREATE_MUTATION, self.doc_id(CREATE_MUTATION),
                                variables)

    def like(self, page_id: str) -> dict[str, Any]:
        """Like a Page (docs/02 §2.10 engagement family).

        Args:
            page_id: The target Page's numeric id; rides input.page_id
                of the decoded PageLikeData shape.

        Returns:
            The merged ``page_like`` response with the like state
            (``page.is_viewer_fan``, ``subscribe_status``).
        """
        variables = _fill(LIKE_VARIABLES, {
            "page_id": page_id,
            "actor_id": self.session.user_id(),
            "attribution": _attribution("PagesCometRoot.react"),
            "client_mutation_id": _mutation_id(),
        })
        return self.client.call(LIKE_MUTATION, self.doc_id(LIKE_MUTATION),
                                variables)

    def follow(self, page_id: str) -> dict[str, Any]:
        """Follow a Page via the decoded actor_subscribe input.

        Args:
            page_id: The target Page's numeric id; rides
                input.subscribee_id of the decoded ActorSubscribeData
                shape.

        Returns:
            The merged ``actor_subscribe`` response with the follow
            state.
        """
        variables = _fill(FOLLOW_VARIABLES, {
            "page_id": page_id,
            "actor_id": self.session.user_id(),
            "attribution": _attribution("PagesCometRoot.react"),
            "client_mutation_id": _mutation_id(),
        })
        return self.client.call(FOLLOW_MUTATION, self.doc_id(FOLLOW_MUTATION),
                                variables)

    # ------------------------------------------------------------------ feeds
    def feed_read(self, page_id_or_vanity: str, *,
                  limit: int = 20) -> FeedPage:
        """Read a page's timeline feed via ProfileCometTimelineListViewRootQuery
        (docs/02 §2.5, docs/15 §P2-2).

        The page SSR-registers the content query with the VERBATIM
        variables the server itself used, with the page id riding as the
        top-level ``userID`` variable: the preload wins when harvestable;
        else the baked DEFAULT_PAGE_FEED_VARIABLES template is used,
        substituting ``userID`` with the numeric argument (a vanity
        argument cannot be resolved offline and relies on the page's own
        preloads). Stories ride as HighlightPostUnit nodes (see
        _parse_page_feed).

        Args:
            page_id_or_vanity: Numeric page id or vanity slug; builds
                the page URL as https://www.facebook.com/<arg>.
            limit: Maximum typed stories to return; the slice happens
                locally so the wire shape is never edited.

        Returns:
            The typed FeedPage for the page timeline.

        Raises:
            ValueError: When a vanity slug is passed but the page fetch
                yielded no preload — a vanity cannot be resolved without
                the page's own preloads; pass the numeric page id.
        """
        entry = None
        try:
            html = self._fetch(f"https://www.facebook.com/{page_id_or_vanity}")
            for candidate in extract_preload_registry(html):
                if candidate.query_name == PAGE_FEED_QUERY:
                    entry = candidate
                    break
        except Exception:
            entry = None
        if entry is not None:
            variables = copy.deepcopy(entry.variables)
            doc_id = entry.doc_id
            if str(page_id_or_vanity).isdigit():
                # the probe showed the page id appears only as top-level userID
                variables["userID"] = page_id_or_vanity
        else:
            if not str(page_id_or_vanity).isdigit():
                raise ValueError(
                    f"page {page_id_or_vanity!r}: a vanity slug cannot be "
                    "resolved without the page's own preloads — pass the "
                    "numeric page id instead")
            variables = _fill(DEFAULT_PAGE_FEED_VARIABLES,
                              {"page_id": page_id_or_vanity})
            doc_id = self.doc_id(PAGE_FEED_QUERY)
        payload = self.client.call(PAGE_FEED_QUERY, doc_id, variables)
        page = _parse_page_feed(payload)
        if limit >= 0 and len(page.stories) > limit:
            page.stories = page.stories[:limit]
        return page
