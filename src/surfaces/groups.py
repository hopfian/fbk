"""Groups surface service: group creation, joining, invites, participation
requests over the GraphQL persisted-query plane (docs/02 §2.7, docs/15 §3).

ARCHITECTURE:

  Feed/members reads follow the any-surface-page law (docs/15 §P3-5):
  GET the group page, harvest the SSR preload registration, replay it
  with the group id substituted as the top-level ``groupID`` variable —
  the verbatim-replay invariant (docs/15 §P2-2) holds for every read.
  Mutations ship schema-decoded templates: the LocalArgument set comes
  from the ``__d("<Name>.graphql")`` module's ``argumentDefinitions``
  and the input object from the JS wrapper that constructs the commit
  variables, with a fresh minified-product attribution string
  (docs/15 §P2-3 wire shape) and client_mutation_id per call. All
  traffic flows through Session/GraphQLClient, so the service is fully
  offline-testable against StubSession (tests/fakes.py).

CALIBRATION NOTES:

  Every mutation template below is schema-decoded (2026-09) from the
  owning rsrc.php bundle of its mutation (the docs/13 §2 bundle-diff
  methodology applied to the /groups/create/ and group permalink chunk
  sets). These are MUTATIONS — the service is real and runnable, but
  live execution is the operator's call (docs/11 §2).

  Decoded argument schemas (bundle-verified, live 2026-09):
  * useGroupsCometCreateMutation      LocalArguments: input, scale
    data: create_group(input: $input) -> CreateGroupResponsePayload.group
  * GroupCometJoinForumMutation       LocalArguments: feedType, groupID,
    input, inviteShortLinkKey, isChainingRecommendationUnit (false),
    renderLocation, scale, source
    data field consumes input; wrapper passes feedType "DISCUSSION",
    renderLocation "group_mall".
  * useGroupAddMembersMutation        LocalArguments: groupID, input
    data: group_add_member(data: $input)
  * GroupsCometRequestToParticipateMutation  LocalArguments: input,
    inviteShortLinkKey, scale
    data: request_to_participate_group(data: $input)

USER-DOC ANCHOR: cli/docs/06-reference-people.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import copy
import re
from typing import Any, Literal

from auth.bootstrap import extract_preload_registry
from domain.common import FeedPage, Story

from .base import Surface, _attribution, _fill, _mutation_id
from .feed import parse_feed_page

CREATE_MUTATION = "useGroupsCometCreateMutation"
JOIN_MUTATION = "GroupCometJoinForumMutation"
ADD_MEMBERS_MUTATION = "useGroupAddMembersMutation"
REQUEST_TO_PARTICIPATE_MUTATION = "GroupsCometRequestToParticipateMutation"

# The group-root FEED query (live-probed 2026-09): GET
# https://www.facebook.com/groups/<id>/ preloads
# CometGroupDiscussionRootSuccessQuery with the VERBATIM variables the server
# itself used (docs/15 §P2-2) — the group id rides as the top-level
# ``groupID`` variable. The group permalink page instead preloads
# CometSinglePostDialogContentQuery (single post, not a feed).
GROUP_FEED_QUERY = "CometGroupDiscussionRootSuccessQuery"
GROUP_POST_DOC_ID = "28650596084564206"

# The group MEMBERS query (live-probed 2026-09): GET
# https://www.facebook.com/groups/<id>/members/ preloads
# GroupsCometMembersRootQuery with the VERBATIM variables {"groupID": <id>,
# "scale": 2}; its replay carries data.group with the member heads:
# group_admin_profiles.edges (admins), new_members.edges (newest members)
# and group_member_profiles.count (the total member count). The doc_id is
# preload-harvested only — not in the bundle registry.
GROUP_MEMBERS_QUERY = "GroupsCometMembersRootQuery"
GROUP_MEMBERS_DOC_ID = "28137485239224972"

# live-captured SSR variables of the members-page preload (2026-09, probe:
# group 12345678901234567 — synthetic in-tree; the real probe id is
# operator-local) — the group id rides only as top-level groupID.
DEFAULT_GROUP_MEMBERS_VARIABLES: dict[str, Any] = {
    "groupID": "{group_id}",
    "scale": 2,
}

VISIBILITIES = ("PUBLIC", "PRIVATE")

#: The members-root query the members page preloads (GroupsComet*MembersRootQuery).
_MEMBERS_QUERY_RE = re.compile(r"GroupsComet\w*MembersRootQuery")

# schema-decoded 2026-09: CreateGroupData as committed by the
# useGroupsCometCreateMutation hook (form defaults: privacy PRIVATE,
# discoverability ANYONE). The decoded input carries no description field.
CREATE_VARIABLES: dict[str, Any] = {
    "input": {
        "attribution_id_v2": "{attribution}",
        "bulk_invitee_members": "{member_ids}",
        "client_mutation_id": "{client_mutation_id}",
        "cover_fbid": None,
        "cover_focus": None,
        "discoverability": "ANYONE",
        "enable_contextual_actors": False,
        "fundraiser_challenge_id": None,
        "group_parent": None,
        "is_forum": False,
        "is_purchaser_automatic_membership_approval_enabled": False,
        "members": "{member_ids}",
        "name": "{name}",
        "package_id": None,
        "privacy": "{visibility}",
        "referrer": "comet",
        "set_affiliation": False,
        "should_invite_followers": False,
    },
    "scale": 2,
}

# schema-decoded 2026-09: GroupCometJoinForumMutation wrapper variables
# (action_source/group_share_tracking_params per the decoded hook input,
# actor_id + client_mutation_id per the analogous live-captured shapes in
# docs/15 §P2-3).
JOIN_VARIABLES: dict[str, Any] = {
    "feedType": "DISCUSSION",
    "groupID": "{group_id}",
    "input": {
        "action_source": "group_mall",
        "actor_id": "{actor_id}",
        "attribution_id_v2": "{attribution}",
        "client_mutation_id": "{client_mutation_id}",
        "group_id": "{group_id}",
        "group_share_tracking_params": None,
    },
    "inviteShortLinkKey": None,
    "isChainingRecommendationUnit": False,
    "renderLocation": "group_mall",
    "scale": 2,
    "source": "group_mall",
}

# schema-decoded 2026-09: useGroupAddMembersMutation hook variables
# (source value from the decoded GroupsCometInviteFriendsDialog call site).
ADD_MEMBERS_VARIABLES: dict[str, Any] = {
    "groupID": "{group_id}",
    "input": {
        "actor_id": "{actor_id}",
        "attribution_id_v2": "{attribution}",
        "client_mutation_id": "{client_mutation_id}",
        "email_addresses": [],
        "group_id": "{group_id}",
        "source": "comet_invite_friends",
        "special_invite_type": None,
        "user_ids": "{member_ids}",
    },
}

# schema-decoded 2026-09: GroupsCometRequestToParticipateMutation wrapper
# variables; answers use the live-captured questionnaire shape
# [{answer, question_id, selected_options}] and are only carried when the
# caller supplies them (the decoded wrapper saves them via a separate
# answers-save mutation otherwise).
REQUEST_TO_PARTICIPATE_VARIABLES: dict[str, Any] = {
    "input": {
        "actor_id": "{actor_id}",
        "answers": "{answers}",
        "attribution_id_v2": "{attribution}",
        "client_mutation_id": "{client_mutation_id}",
        "group_id": "{group_id}",
        "request_creation_source": "comet",
        "source": "group_mall",
    },
    "inviteShortLinkKey": None,
    "scale": 2,
}

# live-probed 2026-09: the full preloader variables for
# CometGroupDiscussionRootSuccessQuery, harvested from the group-root SSR
# registration (docs/15 §P2-2). The group id appears ONLY as the top-level
# ``groupID`` variable (probe: group 12345678901234567, synthetic in-tree),
# so it is the one
# parameterized placeholder. Used whenever a group-page preload is not
# available (e.g. a stubbed session in tests).
DEFAULT_GROUP_FEED_VARIABLES: dict[str, Any] = {
    "autoOpenChat": False,
    "creative_provider_id": None,
    "feedbackSource": 0,
    "feedLocation": "GROUP",
    "feedType": "DISCUSSION",
    "filter_topic_id": None,
    "focusCommentID": None,
    "groupID": "{group_id}",
    "hasHoistStories": False,
    "hoistedSectionHeaderType": "notifications",
    "hoistStories": [],
    "hoistStoriesCount": 0,
    "privacySelectorRenderLocation": "COMET_STREAM",
    "regular_stories_count": 1,
    "regular_stories_stream_initial_count": 1,
    "renderLocation": "group",
    "scale": 2,
    "shouldDeferMainFeed": False,
    "shouldShowBSGAgeVerificationBanner": True,
    "sortingSetting": "TOP_POSTS",
    "threadID": "",
    "useDefaultActor": False,
    "__relay_internal__pv__FBReels_enable_view_dubbed_audio_type_gkrelayprovider": True,
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
    "__relay_internal__pv__GroupsCometGroupChatLazyLoadLastMessageSnippetrelayprovider": False,
    "__relay_internal__pv__groups_comet_use_glvrelayprovider": False,
}


def _dedupe_stories(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop walker stubs and duplicate identities from walked story rows.

    Live symptom (2026-09, group + page feeds): attachment and sub-story
    nodes surface a second, empty Story entry — actor name, text and
    feedback_id all None, only a permalink — echoing the SAME key/id as
    the real story it hangs off. Rule set (rows are ``Story.model_dump()``
    dicts, so the key/feedback ids nest one level deep):
      * a stub (no actor name, no text, no feedback_id) is dropped unless
        it carries a permalink AND a key/id not already retained;
      * a row whose ``(key or id, feedback_id)`` identity was already
        retained is a duplicate echo and dropped; rows without a key/id
        are never identity-deduped.
    """
    out: list[dict[str, Any]] = []
    seen_ids: set[Any] = set()
    seen_pairs: set[tuple[Any, Any]] = set()
    for row in rows:
        actor = row.get("actor")
        has_actor = isinstance(actor, dict) and bool(actor.get("name"))
        feedback = row.get("feedback")
        feedback_id = (feedback.get("id", {}).get("raw")
                       if isinstance(feedback, dict) else None)
        key = row.get("key")
        key_id = (key.get("raw") if isinstance(key, dict) and key.get("raw")
                  else row.get("id"))
        is_stub = (not has_actor and row.get("text") is None
                   and feedback_id is None)
        if is_stub:
            if key_id is None or not row.get("permalink"):
                continue
            if key_id in seen_ids:
                continue
        if key_id is not None:
            if (key_id, feedback_id) in seen_pairs:
                continue
            seen_pairs.add((key_id, feedback_id))
            seen_ids.add(key_id)
        out.append(row)
    return out


def _dedupe_page_stories(stories: list[Story]) -> list[Story]:
    """Run the shared stub/duplicate filter over walked typed stories
    (lossless round-trip through ``Story.model_dump``/``model_validate``;
    shared by the groups and pages feed walkers)."""
    rows = _dedupe_stories([s.model_dump() for s in stories])
    return [Story.model_validate(row) for row in rows]


class GroupsService(Surface):
    """Group surface: feed reads plus create/join/member mutations."""

    def create(
        self,
        name: str,
        *,
        visibility: Literal["PUBLIC", "PRIVATE"] = "PRIVATE",
        description: str | None = None,
        member_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a group via the decoded useGroupsCometCreateMutation
        template (docs/02 §2.7).

        ``visibility`` is the decoded GroupPrivacy value; ``member_ids``
        seeds the decoded input's ``members``/``bulk_invitee_members``
        lists. ``description`` is accepted for API completeness but the
        decoded 2026-09 CreateGroupData input carries no description
        field, so it is not put on the wire.

        Args:
            name: The group name; rides input.name.
            visibility: The GroupPrivacy enum — "PUBLIC" or "PRIVATE"
                (the decoded form default is PRIVATE).
            description: Accepted but never serialized — see above
                (honest omission: the wire shape has no such field).
            member_ids: Optional user ids seeding the invite lists of
                the same mutation call.

        Returns:
            The merged mutation response (``create_group.group``).

        Raises:
            ValueError: If ``visibility`` is not "PUBLIC" or "PRIVATE".
        """
        if visibility not in VISIBILITIES:
            raise ValueError(
                f"visibility must be one of {VISIBILITIES}, got {visibility!r}")
        members = list(member_ids or [])
        variables = _fill(CREATE_VARIABLES, {
            "name": name,
            "visibility": visibility,
            "member_ids": members,
            "attribution": _attribution("GroupsCometCreateRoot.react"),
            "client_mutation_id": _mutation_id(),
        })
        return self.client.call(CREATE_MUTATION, self.doc_id(CREATE_MUTATION),
                                variables)

    def join(self, group_id: str) -> dict[str, Any]:
        """Join a forum-style group (decoded wrapper defaults: feedType
        DISCUSSION, renderLocation group_mall).

        Args:
            group_id: The target group's numeric id; rides both the
                top-level ``groupID`` and input.group_id.

        Returns:
            The merged response with the group membership payload.
        """
        variables = _fill(JOIN_VARIABLES, {
            "group_id": group_id,
            "actor_id": self.session.user_id(),
            "attribution": _attribution("GroupsCometRoot.react"),
            "client_mutation_id": _mutation_id(),
        })
        return self.client.call(JOIN_MUTATION, self.doc_id(JOIN_MUTATION),
                                variables)

    def add_members(self, group_id: str, member_ids: list[str]) -> dict[str, Any]:
        """Invite users to a group by user id (doc_ids per docs/02 §2.7).

        Args:
            group_id: The target group's numeric id.
            member_ids: User ids to invite; ride input.user_ids of the
                decoded ``group_add_member`` input.

        Returns:
            The merged ``group_add_member`` response.
        """
        variables = _fill(ADD_MEMBERS_VARIABLES, {
            "group_id": group_id,
            "actor_id": self.session.user_id(),
            "member_ids": list(member_ids),
            "attribution": _attribution("GroupsCometRoot.react"),
            "client_mutation_id": _mutation_id(),
        })
        return self.client.call(ADD_MEMBERS_MUTATION,
                                self.doc_id(ADD_MEMBERS_MUTATION), variables)

    def request_to_participate(
        self, group_id: str, *, answers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Request to join a gated group.

        Args:
            group_id: The target group's numeric id.
            answers: Optional membership-question answers mapping
                question id -> free-text answer, encoded into the
                live-captured questionnaire wire shape
                [{answer, question_id, selected_options}]; when absent
                the request is submitted without an answers payload
                (mirroring the decoded wrapper, which saves answers via
                a separate mutation).

        Returns:
            The merged ``request_to_participate_group`` response.
        """
        values: dict[str, Any] = {
            "group_id": group_id,
            "actor_id": self.session.user_id(),
            "attribution": _attribution("GroupsCometRoot.react"),
            "client_mutation_id": _mutation_id(),
            "answers": [
                {"answer": text, "question_id": question_id,
                 "selected_options": []}
                for question_id, text in (answers or {}).items()
            ],
        }
        variables = _fill(REQUEST_TO_PARTICIPATE_VARIABLES, values)
        if answers is None:
            del variables["input"]["answers"]
        return self.client.call(
            REQUEST_TO_PARTICIPATE_MUTATION,
            self.doc_id(REQUEST_TO_PARTICIPATE_MUTATION), variables)

    # ------------------------------------------------------------------ feeds
    def feed_read(self, group_id: str, *, cursor: str | None = None,
                  limit: int = 20) -> FeedPage:
        """Read the group-root feed via CometGroupDiscussionRootSuccessQuery
        (docs/02 §2.7, docs/15 §P2-2).

        Live-probed 2026-09: GET /groups/<id>/ SSR-registers the group feed
        query with the VERBATIM variables the server itself used, with the
        group id riding as the top-level ``groupID`` variable. Variable
        precedence: the page preload when harvestable (with the requested
        group id substituted into ``groupID``), else the baked
        DEFAULT_GROUP_FEED_VARIABLES template. The payload is walked by
        the shared feed walker (surfaces/feed.parse_feed_page); walked
        stories then pass the shared stub/duplicate filter
        (_dedupe_stories — live-observed attachment sub-nodes echo an
        empty duplicate Story entry, docs/15 §P5-5).

        Args:
            group_id: The group's numeric id.
            cursor: Optional opaque pagination token from the previous
                page's end_cursor (docs/04 §5 convention), merged as a
                top-level variable.
            limit: Maximum typed stories to return; the slice happens
                locally so the wire shape is never edited.

        Returns:
            The typed FeedPage for the group feed.
        """
        entry = None
        try:
            html = self._fetch(f"https://www.facebook.com/groups/{group_id}/")
            for candidate in extract_preload_registry(html):
                if candidate.query_name == GROUP_FEED_QUERY:
                    entry = candidate
                    break
        except Exception:
            entry = None
        if entry is not None:
            variables = copy.deepcopy(entry.variables)
            doc_id = entry.doc_id
        else:
            variables = _fill(DEFAULT_GROUP_FEED_VARIABLES,
                             {"group_id": group_id})
            doc_id = self.doc_id(GROUP_FEED_QUERY)
        # the probe showed the group id appears only as top-level groupID
        variables["groupID"] = group_id
        if cursor is not None:
            variables["cursor"] = cursor
        payload = self.client.call(GROUP_FEED_QUERY, doc_id, variables)
        page = parse_feed_page(payload)
        page.stories = _dedupe_page_stories(page.stories)
        if limit >= 0 and len(page.stories) > limit:
            page.stories = page.stories[:limit]
        return page

    def members(self, group_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """Read a group's member heads via GroupsCometMembersRootQuery
        (live pattern, read-only; docs/15 §P2-2).

        Live-probed 2026-09: GET /groups/<id>/members/ SSR-registers the
        members root query with the VERBATIM variables the server itself
        used (the group id rides as the top-level ``groupID`` variable).
        Variable precedence: the page preload when harvestable, else the
        baked DEFAULT_GROUP_MEMBERS_VARIABLES template.

        Args:
            group_id: The group's numeric id.
            limit: Maximum rows to return; rows are deduped on id and
                sliced by ``limit`` locally so the wire shape is never
                edited.

        Returns:
            User-like dicts — id, name, role, is_self — headed by the
            admins (``group_admin_profiles`` edges, role ADMIN) then
            the newest members (``new_members`` edges); the probe
            account's live replay also exposed the total member count
            on ``group_member_profiles.count`` (docs/15 §P8-2).
        """
        entry = None
        try:
            html = self._fetch(f"https://www.facebook.com/groups/{group_id}/members/")
            for candidate in extract_preload_registry(html):
                if (candidate.query_name
                        and _MEMBERS_QUERY_RE.fullmatch(candidate.query_name)):
                    entry = candidate
                    break
        except Exception:
            entry = None
        if entry is not None:
            variables = copy.deepcopy(entry.variables)
            doc_id = entry.doc_id
        else:
            variables = _fill(DEFAULT_GROUP_MEMBERS_VARIABLES,
                             {"group_id": group_id})
            doc_id = GROUP_MEMBERS_DOC_ID
        # the probe showed the group id appears only as top-level groupID
        variables["groupID"] = group_id
        payload = self.client.call(GROUP_MEMBERS_QUERY, doc_id, variables)
        rows = self._walk_members(payload)
        if limit >= 0 and len(rows) > limit:
            rows = rows[:limit]
        return rows

    def _walk_members(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        """User-like rows off the members root replay (live-observed shape:
        data.group.group_admin_profiles.edges / new_members.edges, node =
        User with id + name). Admin rows keep role ADMIN and head the list
        (the page renders them first); duplicates on id are dropped."""
        group = (payload.get("data") or {}).get("group")
        if not isinstance(group, dict):
            return []
        viewer_id = self.session.user_id()
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()

        def add(node: Any, role: str | None) -> None:
            if not isinstance(node, dict) or node.get("__typename") != "User":
                return
            user_id = str(node.get("id") or "")
            if not user_id or user_id in seen:
                return
            seen.add(user_id)
            rows.append({
                "id": user_id,
                "name": node.get("name"),
                "role": role,
                "is_self": user_id == viewer_id,
            })

        for section, role in (("group_admin_profiles", "ADMIN"),
                             ("new_members", None)):
            connection = group.get(section)
            if not isinstance(connection, dict):
                continue
            edges = connection.get("edges")
            if not isinstance(edges, list):
                continue
            for edge in edges:
                if isinstance(edge, dict):
                    add(edge.get("node"), role)
        return rows
