"""Profile surface service (docs/02 §2.5, docs/15-live-calibration-findings.md).

Own-profile identity reads and arbitrary-profile timeline reads via the
preload-replay strategy.

ARCHITECTURE:

  The /me page is a logged-in Comet page whose preload registry carries the
  profile queries with VERBATIM variables (docs/15 §P2-2 methodology):
  replay one, walk the payload for viewer/profile fields, and fall back to
  the home bootstrap identity (CurrentUserInitialData — docs/15 §2) when
  the page fetch or the replay fails. The other-user leg
  (:meth:`ProfileService.view`) fetches the target's profile page, harvests
  its ``ProfileCometTimelineListViewRootQuery`` preload, substitutes the
  numeric id over the preloaded ``userID``, and parses stories with the
  pages-surface feed walker — the two surfaces share one story-node shape
  (docs/15 §P4-4 page-feed finding).

CALIBRATION NOTES:

  * ``ProfileCometTimelineFeedQuery`` errors partially even verbatim; the
    clean-replaying content query is ``ProfileCometTimelineListViewRootQuery``
    (28117370721250101) with the page id as top-level ``userID``
    (docs/15 §P4-4).
  * The list-view selection carries no bare ``user.name`` field — the
    identity head is harvested from the payload's own story-actor nodes
    (live-probed 2026-09 on the sample-page replay).

USER-DOC ANCHOR: cli/docs/04-reference-feed.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""

from __future__ import annotations

import copy
from typing import Any

from pydantic import BaseModel

from auth.bootstrap import PreloadEntry, extract_preload_registry
from domain.common import FeedPage, Profile, User

from .base import Surface
from .pages import _parse_page_feed

#: Own-profile page URL — GET /me redirects to the canonical profile URL;
#: the redirect target's HTML carries the preload registry.
ME_URL = "https://www.facebook.com/me"

#: Arbitrary-profile page template — vanity slug or numeric id (docs/02 §2.5).
PROFILE_URL = "https://www.facebook.com/{user}"

#: The clean-replaying timeline content query (docs/15 §P4-4) plus its
#: live-harvested doc_id and the baked variable template (preload fallback).
DEFAULT_PROFILE_QUERY = "ProfileCometTimelineListViewRootQuery"
DEFAULT_PROFILE_DOC_ID = "28117370721250101"
DEFAULT_PROFILE_VARIABLES: dict[str, Any] = {
    "previousProfileId": None,
    "privacySelectorRenderLocation": "COMET_STREAM",
    "renderLocation": "timeline",
    "scale": 2,
    "userID": "12345678901234",
    "__relay_internal__pv__GHLShouldChangeSponsoredDataFieldNamerelayprovider": True,
    "__relay_internal__pv__WorkCometIsEmployeeGKProviderrelayprovider": False,
    "__relay_internal__pv__GroupsCometGroupChatLazyLoadLastMessageSnippetrelayprovider": False,
    "__relay_internal__pv__ProfileCometFeaturedHighlightsPortraitAspectRatioGKrelayprovider": False,
    "__relay_internal__pv__WebPixelRatiorelayprovider": 2,
}


class ProfileView(BaseModel):
    """One profile's identity head plus its timeline feed page.

    The identity head (id + name) rides the replayed payload's own nodes
    (story actor nodes carry the target's id + name — the list-view query
    selection does not include a bare ``user.name`` field, live-probed
    2026-09 on the sample-page replay)."""

    user: User
    feed: FeedPage


def _trim(node: Any, levels: int = 1) -> Any:
    """Keep the top `levels` of a payload; deeper dicts collapse to their key
    lists, lists to a length fingerprint (journal/emit-safe views)."""

    def cut(n: Any, depth: int) -> Any:
        if isinstance(n, dict):
            if depth >= levels:
                return sorted(n.keys())
            return {k: cut(v, depth + 1) for k, v in n.items()}
        if isinstance(n, list):
            if depth >= levels:
                return f"<list:{len(n)}>"
            return [cut(v, depth + 1) for v in n[:4]]
        return n

    return cut(node, 0)


def _walk_profile_fields(payload: Any, uid: str) -> dict[str, Any]:
    """Collect viewer/profile fields (id, name, bio, photo, business markers)
    from any node of the replayed payload whose id matches the viewer."""
    found: dict[str, Any] = {}

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("id") == uid:
                if "id" not in found:
                    found["id"] = uid
                for src, dst in (("name", "name"), ("short_name", "short_name"), ("bio", "bio")):
                    if isinstance(node.get(src), str) and dst not in found:
                        found[dst] = node[src]
                for pk in ("profile_picture", "profile_photo"):
                    ph = node.get(pk)
                    if isinstance(ph, dict) and "photo" not in found:
                        uri = ph.get("uri")
                        if isinstance(uri, str):
                            found["photo"] = uri
                for k, v in node.items():
                    if "professional" in k and v is True:
                        found["is_business"] = True
            for v in node.values():
                visit(v)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(payload)
    return found


class ProfileService(Surface):
    """Own-profile reads grounded in the /me Comet surface (docs/02 §2.5)."""

    def _preload_candidates(self, uid: str) -> list[PreloadEntry]:
        """Profile-query preload entries for /me, DEFAULT query first."""
        try:
            html = self._fetch(ME_URL)
        except Exception:
            return []
        entries = extract_preload_registry(html)
        profiled = [
            e
            for e in entries
            if e.query_name and "Profile" in e.query_name and uid in str(e.variables)
        ]
        profiled += [
            e for e in entries if e.query_name and "Profile" in e.query_name and e not in profiled
        ]
        profiled.sort(key=lambda e: 0 if e.query_name == DEFAULT_PROFILE_QUERY else 1)
        return profiled

    def me(self) -> Profile:
        """The logged-in user's own profile.

        Enriches the bootstrap identity with a live replay of the /me preload
        registry's main profile query; on any fetch/replay failure the
        bootstrap identity (always available — docs/15 §2) is returned with
        raw={}.

        Returns:
            A ``Profile`` whose ``user.is_self`` is always True; ``raw``
            carries a depth-trimmed view of the best replayed payload
            (journal/emit-safe — see :func:`_trim`), or ``{}`` on the
            bootstrap fallback.

        Raises:
            Nothing from the replay legs — every candidate fetch/replay
            failure degrades to the bootstrap identity; only session-level
            construction errors can surface.
        """
        uid = self.session.user_id()
        boot = self.session.bootstrap()
        best: dict[str, Any] = {}
        best_raw: dict[str, Any] = {}
        for entry in self._preload_candidates(uid):
            try:
                data = self.client.call(entry.query_name or "", entry.doc_id, entry.variables)
            except Exception:
                continue
            fields = _walk_profile_fields(data.get("data") or {}, uid)
            if fields.get("id") and fields.get("name"):
                best, best_raw = fields, _trim(data.get("data") or {}, 1)
                break
            if fields.get("id") and not best:
                best, best_raw = fields, _trim(data.get("data") or {}, 1)
        if not best:
            return Profile(
                user=User(id=uid, name=boot.user_name, is_self=True),
                is_business=False,
                raw={},
            )
        return Profile(
            user=User(
                id=best.get("id") or uid, name=best.get("name") or boot.user_name, is_self=True
            ),
            is_business=bool(best.get("is_business")),
            raw=best_raw,
        )

    # ------------------------------------------------------------ other users
    def view(self, user_id_or_vanity: str, *, limit: int = 10) -> ProfileView:
        """Read ANY profile's timeline feed (docs/02 §2.5, docs/15 §P2-2).

        ``user_id_or_vanity`` builds the profile URL as
        https://www.facebook.com/<arg> (numeric id or vanity slug). The page
        SSR-registers ``ProfileCometTimelineListViewRootQuery`` with the
        VERBATIM variables the server itself used — the target's id rides as
        the top-level ``userID`` variable (live-probed 2026-09 on
        /sample.page). The preload wins when harvestable; a numeric argument
        is substituted over the preloaded ``userID`` (the preload for any
        profile page gives the full working variable shape); without a
        preload the baked DEFAULT_PROFILE_VARIABLES template carries a
        numeric argument (a vanity slug cannot be resolved offline). The
        replayed payload is walked with the pages-surface story parser
        (HighlightPostUnit nodes — the same shape the pages feed reads),
        and the target's identity head is harvested from the payload's own
        nodes via _walk_profile_fields (the target's story actor nodes
        carry its id + name).

        Args:
            user_id_or_vanity: The profile target — a numeric user id or a
                vanity slug (``https://www.facebook.com/<arg>``).
            limit: Story cap; negative means unlimited (no truncation).

        Returns:
            A ``ProfileView`` — the target's identity head (id + name, the
            name only when a story-actor node carried it) plus the typed
            timeline feed page (``FeedPage``).

        Raises:
            ValueError: When a VANITY slug is passed but the profile page
                carried no harvestable preload — a slug cannot be resolved
                offline into the numeric ``userID`` the query requires;
                pass the numeric id instead.
            FBGraphError: Typed session/checkpoint/rate-limit errors from
                the GraphQL client propagate unchanged.
        """
        entry: PreloadEntry | None = None
        try:
            html = self._fetch(PROFILE_URL.format(user=user_id_or_vanity))
            for candidate in extract_preload_registry(html):
                if candidate.query_name == DEFAULT_PROFILE_QUERY:
                    entry = candidate
                    break
        except Exception:
            entry = None
        if entry is not None:
            variables = copy.deepcopy(entry.variables)
            doc_id = entry.doc_id
            if str(user_id_or_vanity).isdigit():
                variables["userID"] = user_id_or_vanity
        else:
            if not str(user_id_or_vanity).isdigit():
                raise ValueError(
                    f"profile {user_id_or_vanity!r}: a vanity slug cannot "
                    "be resolved without the profile page's own preloads — "
                    "pass the numeric user id instead"
                )
            variables = copy.deepcopy(DEFAULT_PROFILE_VARIABLES)
            variables["userID"] = user_id_or_vanity
            doc_id = self.doc_id(DEFAULT_PROFILE_QUERY)
        payload = self.client.call(DEFAULT_PROFILE_QUERY, doc_id, variables)
        data = payload.get("data") or {} if isinstance(payload, dict) else {}
        feed = _parse_page_feed(payload)
        target_id = str(variables.get("userID") or "")
        fields = _walk_profile_fields(data, target_id)
        if limit >= 0 and len(feed.stories) > limit:
            feed.stories = feed.stories[:limit]
        return ProfileView(
            user=User(id=target_id or str(fields.get("id") or ""), name=fields.get("name")),
            feed=feed,
        )
