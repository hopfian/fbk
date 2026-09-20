"""Search surface (docs/02-endpoint-surface-map.md §search; docs/15 §P2-2, §P3-5).

ARCHITECTURE:

  Follows the any-surface-page law (docs/15 §P3-5): GET /search/top,
  harvest the SSR preload registration, replay it verbatim, walk the
  merged payload for typed result nodes. Search is the most
  integrity-gated read surface (docs/02 §2.4), so the verbatim replay —
  never a re-assembled variables dict — is the fidelity/bypass-resistance
  discipline. All traffic flows through Session/GraphQLClient, so the
  service is fully offline-testable against StubSession (tests/fakes.py).

CALIBRATION NOTES:

  Live-verified pattern (docs/15 §P3-5): GET /search/top?q=<urlencoded>
  embeds ``SearchCometResultsInitialResultsQuery`` (doc_id
  29118914987711900) plus its VERBATIM variables in the page preload
  registry (docs/15 §P2-2); replaying that pair yields a ~1.2MB typed
  results payload. The parallel-fetch sibling
  (``SearchCometResultsInitialResultsParallelFetchQuery``) is an ADS
  parallel-fetch query that returns an empty envelope and is deliberately
  ignored.

USER-DOC ANCHOR: cli/docs/05-reference-content.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import copy
import json
from typing import Any
from urllib.parse import quote_plus

from auth.bootstrap import extract_preload_registry
from domain.common import SearchResponse, SearchResult

from .base import Surface

SEARCH_URL = "https://www.facebook.com/search/top"
SEARCH_QUERY_NAME = "SearchCometResultsInitialResultsQuery"
SEARCH_FALLBACK_DOC_ID = "29118914987711900"

#: The live-captured SSR variables of the search preload (docs/15 §P3-5),
#: harvested from assets/search_page_sample.html. Used verbatim when the
#: fetched page carries the preload, and as the template (with ``args.text``
#: swapped for the user query) when it does not.
DEFAULT_SEARCH_VARIABLES: dict[str, Any] = {
    "count": 5,
    "allow_streaming": False,
    "args": {
        "callsite": "COMET_GLOBAL_SEARCH",
        "config": {
            "exact_match": False,
            "high_confidence_config": None,
            "intercept_config": None,
            "sts_disambiguation": None,
            "watch_config": None,
        },
        "context": {"bsid": "ad535570-33ad-428f-86f8-d1ae7413d488", "tsid": None},
        "experience": {
            "client_defined_experiences": ["ADS_PARALLEL_FETCH"],
            "encoded_server_defined_params": None,
            "fbid": None,
            "type": "GLOBAL_SEARCH",
        },
        "filters": [],
        "text": "python programming",
    },
    "cursor": None,
    "feedbackSource": 23,
    "fetch_filters": True,
    "renderLocation": "search_results_page",
    "scale": 2,
    "stream_initial_count": 0,
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

#: Node typenames the search payload carries with a human name (docs/15 §P3-5).
_RESULT_TYPENAMES = frozenset({"User", "Page", "Group", "Story", "Video"})

#: Shallow node keys probed for a snippet (any nearby 'text' field).
_SNIPPET_KEYS = ("text", "subtitle", "category_type", "description", "city")


class SearchService(Surface):
    """Top-search surface: page GET -> preload replay -> typed results."""

    # ------------------------------------------------------------------ public
    def search(self, query: str, *, limit: int = 20) -> SearchResponse:
        """Search Facebook's top results for ``query`` (docs/15 §P3-5).

        GETs /search/top, harvests the SearchCometResultsInitialResultsQuery
        preload entry (verbatim variables, docs/15 §P2-2), replays it, and
        walks the merged payload for typed User/Page/Group/Story/Video
        nodes. Falls back to the live-baked DEFAULT_SEARCH_VARIABLES
        (with ``args.text`` set to ``query``) when the page carries no
        preload.

        Args:
            query: The search term; urlencoded into the page URL (the
                preloaded query itself carries the term inside
                args.text — verbatim replay, never re-assembled).
            limit: Maximum results to return; the typed rows are sliced
                locally so the wire shape is never edited.

        Returns:
            The SearchResponse: typed, id-deduped result rows plus
            ``raw_size`` for soft-block logging; an empty result set
            passes (account-state, not a protocol failure).
        """
        html = self._fetch(f"{SEARCH_URL}?q={quote_plus(query)}")
        entry = None
        for candidate in extract_preload_registry(html):
            if candidate.query_name == SEARCH_QUERY_NAME:
                entry = candidate
                break
        if entry is not None:
            doc_id: str = entry.doc_id
            variables: dict[str, Any] = dict(entry.variables)
        else:
            doc_id = SEARCH_FALLBACK_DOC_ID
            variables = copy.deepcopy(DEFAULT_SEARCH_VARIABLES)
            args = variables.get("args")
            if isinstance(args, dict):
                args["text"] = query
        data = self.client.call(SEARCH_QUERY_NAME, doc_id, variables)
        results = self._walk_results(data)
        return SearchResponse(
            query=query,
            results=results[:limit],
            raw_size=len(json.dumps(data)),
        )

    # ----------------------------------------------------------------- parsing
    @staticmethod
    def _snippet(node: dict[str, Any]) -> str | None:
        """A nearby human-text field on a result node (any of _SNIPPET_KEYS)."""
        for key in _SNIPPET_KEYS:
            value = node.get(key)
            if isinstance(value, str) and value:
                return value
        return None

    @classmethod
    def _walk_results(cls, data: dict[str, Any]) -> list[SearchResult]:
        """Collect named User/Page/Group/Story/Video nodes, deduped on id."""
        out: list[SearchResult] = []
        seen: set[tuple[str, str]] = set()

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                typename = node.get("__typename")
                if typename in _RESULT_TYPENAMES and node.get("name"):
                    key = (str(typename), str(node.get("id") or node.get("name")))
                    if key not in seen:
                        seen.add(key)
                        out.append(SearchResult(
                            typename=str(typename),
                            name=str(node["name"]),
                            id=str(node["id"]) if node.get("id") else None,
                            url=(node.get("url") or node.get("profile_url") or None),
                            snippet=cls._snippet(node),
                        ))
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(data)
        return out
