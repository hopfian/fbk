"""Events surface service (docs/02 §2 endpoint families, docs/15 §P2-2).

ARCHITECTURE:

  List reads follow the any-surface-page law (docs/15 §P3-5): GET
  /events/, harvest the SSR preload registration, replay it verbatim.
  Feed reads replay the schema-decoded LocalArgument template with the
  Relay node id substituted and the shared feed walker extracting typed
  stories. Writes (delete, create) ship schema-decoded committer
  templates with the live-observed action-context blob; the decoded
  schemas carry no client_mutation_id, so none is invented. All traffic
  flows through Session/GraphQLClient, so the service is fully
  offline-testable against StubSession (tests/fakes.py).

CALIBRATION NOTES — ground truth (live-probed 2026-09-18, read-only):

* LIST — GET https://www.facebook.com/events/ SSR-registers
  ``EventCometHomeRootQuery`` (doc_id 38585542624394211, live preload +
  delta-harvested relayOperation) with the VERBATIM variables the server
  itself used (docs/15 §P2-2). Replaying them returns
  ``viewer.actor.upcoming_events.edges[].node`` plus
  ``viewer.actor.content_tab.requested_tab.events.edges[].node`` —
  Event nodes carrying id / name / day_time_sentence / start_timestamp /
  is_past / is_viewer_host (base field set decoded from the owning bundle
  Fv-K2-wFoT9su8wdxV1UomTl9TctA1NM0kOkm.js).
* FEED — ``EventCometPermalinkDiscussionFeedPaginationQuery``
  (doc_id 28948528681410727, registry). LocalArguments schema-decoded from
  the owning bundle YfnmhkZIj7t.js: count(3), cursor, feedLocation,
  feedbackSource, focusCommentID, hoistedIDs, id, privacySelectorRenderLocation,
  referringStoryRenderLocation, renderLocation, scale, useDefaultActor.
  The operation is ``node(id: $id) { event_stories(first: $count,
  after: $cursor) { edges { node } page_info } }``; the Event node id is
  the Relay form — base64 of "Event:<numeric>" (docs/14 identifier table).
  Not live-replayed end-to-end: the probe account has zero events, so the
  id substitution is schema-derived (honest omission — no invented wire).
* DELETE — ``useEventCometDeleteMutation`` (doc_id 28037817465841827,
  registry). Schema fully decoded from the owning bundle eWu93ntaByI.js:
  LocalArguments ``input`` + ``scale``; the hook commits
  ``{input: {acontext: <EventCometActionContext>, event_id: <eventID>},
  scale: <WebPixelRatio>}`` and the response carries
  ``event_cancel.canceled_event_id`` (plus canceled_child_event_ids /
  group_id / page_id / parent_event_id). The acontext below is the
  LIVE-OBSERVED action-history blob from /events/ rail links. Own events
  only — the server rejects deletes of non-hosted events. NOT live-fired.
* CREATE — ``EventCometLightweightCreateMutation`` (doc_id 24003471242569934,
  discovered via delta-harvest of the /events/create/ bundle set 2026-09-18
  and added to the registry). LocalArgument ``input``; the committer
  (bundle ...SwboX64UU_HYbKcK5rtx3t30pkOwXufFZ1AQYkEUiJ99iASUQ4Wnm.js)
  builds a ~45-key input object and commits
  ``fb_event_create(input: $input) -> Event { id }``. NOT live-fired —
  the template below is the schema-decoded committer shape with the CLI
  fields substituted.
* RSVP — the RSVP UI is a deferred chunk (``requireDeferred(
  "PublicEventCometRSVPMutation")`` observed in the RSVP-button bundle);
  its doc_id is NOT harvestable from any served page or bundle in the
  /events/ surface delta (honest omission — no RSVP method is exposed).

USER-DOC ANCHOR: cli/docs/05-reference-content.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import base64
import copy
from typing import Any

from pydantic import BaseModel, Field

from .base import Surface
from .feed import parse_feed_page

EVENTS_URL = "https://www.facebook.com/events/"

# ----------------------------------------------------------------------- list
LIST_QUERY = "EventCometHomeRootQuery"
LIST_DOC_ID = "38585542624394211"

#: Live-probed 2026-09-18: the verbatim SSR preloader variables for the
#: /events/ page (docs/15 §P2-2). Used whenever no bootstrap preload entry
#: is available (e.g. a stubbed session in tests).
DEFAULT_EVENTS_VARIABLES: dict[str, Any] = {
    "discoverTab": "RECOMMENDED",
    "enableHScroll": False,
    "endTimestamp": None,
    "eventFlags": None,
    "hasHoistedEventIDs": False,
    "hoistedEventIDs": [],
    "initialCount": 6,
    "locationID": None,
    "scale": 2,
    "startTimestamp": None,
}

# ----------------------------------------------------------------------- feed
FEED_QUERY = "EventCometPermalinkDiscussionFeedPaginationQuery"
FEED_DOC_ID = "28948528681410727"

#: Schema-decoded LocalArguments of EventCometPermalinkDiscussionFeedPagination
#: Query (owning bundle YfnmhkZIj7t.js, 2026-09-18): every argument with its
#: decoded default, plus scale=2 / useDefaultActor=False as the web client
#: passes them (WebPixelRatio / useDefaultActor conventions, docs/15 §P2-2).
DEFAULT_EVENT_FEED_VARIABLES: dict[str, Any] = {
    "count": 3,
    "cursor": None,
    "feedLocation": None,
    "feedbackSource": None,
    "focusCommentID": None,
    "hoistedIDs": None,
    "id": None,
    "privacySelectorRenderLocation": None,
    "referringStoryRenderLocation": None,
    "renderLocation": None,
    "scale": 2,
    "useDefaultActor": False,
}

# ---------------------------------------------------------------------- delete
DELETE_MUTATION = "useEventCometDeleteMutation"
DELETE_DOC_ID = "28037817465841827"

#: LIVE-OBSERVED EventCometActionContext (URL-decoded from /events/ rail
#: links, 2026-09-18): {"event_action_history":[{"mechanism":"left_rail",
#: "surface":"bookmark"}],"ref_notif_type":null}.
EVENTS_ACTION_CONTEXT: dict[str, Any] = {
    "event_action_history": [{"mechanism": "left_rail", "surface": "bookmark"}],
    "ref_notif_type": None,
}

#: Schema-decoded 2026-09-18 (bundle eWu93ntaByI.js): the hook commits
#: {input: {acontext, event_id}, scale: WebPixelRatio.get()}. The schema
#: carries NO client_mutation_id (none is substituted or invented).
DELETE_VARIABLES_TEMPLATE: dict[str, Any] = {
    "input": {
        "acontext": copy.deepcopy(EVENTS_ACTION_CONTEXT),
        "event_id": None,
    },
    "scale": 2,
}

# ---------------------------------------------------------------------- create
CREATE_MUTATION = "EventCometLightweightCreateMutation"
CREATE_DOC_ID = "24003471242569934"

#: Schema-decoded 2026-09-18 from the EventCometLightweightCreateMutation
#: committer (bundle ...SwboX64UU...Wnm.js): the complete input object the
#: web dialog assembles, with the CLI-substitutable fields as None. Fields
#: the form state cannot reach for a plain single in-person event are the
#: decoded neutral defaults; the schema has NO client_mutation_id.
CREATE_INPUT_TEMPLATE: dict[str, Any] = {
    "acontext": copy.deepcopy(EVENTS_ACTION_CONTEXT),
    "associated_horizon_world_id": None,
    "attribution_id_v2": None,
    "can_message_host": None,
    "category_id": None,
    "child_events_data": None,
    "cohost_ids_added": [],
    "cover_focus": None,
    "cover_photo_id": None,
    "cover_photo_type": None,
    "cover_video_id": None,
    "creation_end_screen": "CREATION_SCREEN",
    "creation_entrypoint": "others",  # live /events/create/ preload dialogEntryPoint
    "creation_start_screen": None,
    "description": None,
    "disable_guest_invite": False,
    "end_date": None,
    "end_time": None,
    "event_chat": {
        "enable_event_chat": False,
        "initial_message": None,
        "is_chat_suggestion": False,
        "link_existing_chat_id": None,
    },
    "event_frequency": None,
    "event_privacy_type": "PUBLIC_TYPE",
    "flags": None,
    "gif_cover_photo_id": None,
    "group_id": None,
    "hide_guest_list": False,
    "interception_id": None,
    "invite_group_members": None,
    "is_horizon_public_instance": None,
    "llm_prefilled_data": None,
    "location_id": None,
    "location_latlong": None,
    "location_name": None,
    "multi_cover_media": None,
    "name": None,
    "online_third_party_url": None,
    "only_admins_can_post": False,
    "post_approval_required": None,
    "post_id": None,
    "save_as_draft": None,
    "selected_event_type": "UNSPECIFIED",
    "spam_folder_setting": None,
    "start_date": None,
    "start_time": None,
    "theme_photo_id": None,
    "ticket_link": None,
    "timezone": None,
}

#: Privacy enum values the decoded committer compares against
#: (PUBLIC_TYPE / PRIVATE_TYPE / GROUP are the only literals in the bundle).
EVENT_PRIVACY_TYPES = ("PUBLIC_TYPE", "PRIVATE_TYPE", "GROUP")


# ------------------------------------------------------------------------ typing
class EventRow(BaseModel):
    """One event row from the /events/ list.

    The base field set decoded from the owning bundle's Event selection
    (id, name, day_time_sentence, start_timestamp, is_past,
    is_viewer_host); the optional going-count and typename/source fields
    cover the card variants the live payload attaches.
    """

    id: str
    name: str | None = None
    date_text: str | None = None
    start_timestamp: int | None = None
    is_past: bool | None = None
    is_viewer_host: bool | None = None
    going_count: int | None = None
    typename: str | None = None
    source: str | None = None


class EventFeedPage(BaseModel):
    """One page of an event's discussion feed plus pagination plumbing.

    The stories ride as raw Story dicts (the shared feed walker's typed
    rows, model_dumped) alongside the connection's opaque end_cursor /
    has_next_page pair and the raw_size soft-block signature.
    """

    stories: list[dict[str, Any]] = Field(default_factory=list)
    end_cursor: str | None = None
    has_next_page: bool = False
    raw_size: int = 0


def event_node_id(event_id: str) -> str:
    """Event target -> the Relay node id for the feed query's ``id`` argument.

    Accepts the base64 form ('RXZlbnQ6…' — base64 of "Event:<numeric>",
    docs/14 identifier table) or a bare numeric fbid and normalizes to the
    base64 wire form.

    Args:
        event_id: A numeric event fbid or an already-encoded Relay node
            id; non-numeric input is passed through unchanged (assumed
            pre-encoded).

    Returns:
        The base64 wire form of the "Event:<numeric>" Relay node id,
            padding-stripped the way the server emits it.
    """
    if event_id.isdigit():
        return base64.b64encode(f"Event:{event_id}".encode()).decode().rstrip("=")
    return event_id


def _going_count(node: dict[str, Any]) -> int | None:
    """The going-count when the payload variant carries one (else None).

    The decoded base Event selection has no going-count field; some card
    variants attach ``going_count`` / ``event_going_count``-style scalars,
    so any int scalar whose name ends in "going" is honored ("if present").
    """
    for key, val in node.items():
        if "going" not in key.lower():
            continue
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            continue
        return int(val)
    return None


def _event_row(node: dict[str, Any], *, source: str) -> EventRow:
    date_text = node.get("day_time_sentence")
    if not isinstance(date_text, str):
        date_text = None
    start = node.get("start_timestamp")
    return EventRow(
        id=str(node.get("id") or ""),
        name=node.get("name") if isinstance(node.get("name"), str) else None,
        date_text=date_text,
        start_timestamp=start if isinstance(start, int) else None,
        is_past=node.get("is_past") if isinstance(node.get("is_past"), bool) else None,
        is_viewer_host=(node.get("is_viewer_host")
                        if isinstance(node.get("is_viewer_host"), bool) else None),
        going_count=_going_count(node),
        typename=node.get("__typename") if isinstance(node.get("__typename"), str) else None,
        source=source,
    )


def parse_event_rows(payload: dict[str, Any]) -> list[EventRow]:
    """Merged /events/ payload -> typed EventRows (never raises).

    Rows come from ``viewer.actor.upcoming_events.edges[].node`` (own
    events) then ``viewer.actor.content_tab.requested_tab.events.edges[]
    .node`` (discover content), deduped on id (live-decoded 2026-09-18).

    Args:
        payload: The merged EventCometHomeRootQuery response document.

    Returns:
        Typed EventRows in document order (own events first, then
        discover rows); an unusable payload yields an empty list, never
        an exception.
    """
    out: list[EventRow] = []
    seen: set[str] = set()
    viewer = payload.get("data", {}).get("viewer", {})
    actor = viewer.get("actor") if isinstance(viewer, dict) else None
    if not isinstance(actor, dict):
        return out
    connections = [
        (actor.get("upcoming_events"), "upcoming_events"),
        ((actor.get("content_tab") or {}).get("requested_tab", {})
         .get("events") if isinstance(actor.get("content_tab"), dict)
         else None, "discover"),
    ]
    for conn, source in connections:
        edges = conn.get("edges") if isinstance(conn, dict) else None
        if not isinstance(edges, list):
            continue
        for edge in edges:
            if not isinstance(edge, dict):
                continue
            node = edge.get("node")
            if not isinstance(node, dict) or not node.get("id"):
                continue
            if node["id"] in seen:
                continue
            seen.add(str(node["id"]))
            out.append(_event_row(node, source=source))
    return out


# ------------------------------------------------------------------------ service
class EventsService(Surface):
    """Events surface: list / discussion feed / delete / create (docs/02 §2).

    Reads replay the server's own preloader variables (docs/15 §P2-2);
    writes ship the schema-decoded committer templates with no invented
    fields — every schema carries exactly what the decoded hook commits.
    """

    # ------------------------------------------------------------------ reads
    def list(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """The /events/ list, replayed with the page's verbatim variables.

        docs/15 §P2-2: the SSR registration carries the exact variables the
        server used — that entry wins; the baked DEFAULT_EVENTS_VARIABLES
        (live-probed) are the fallback.

        Args:
            limit: Maximum rows to return; the typed rows are trimmed
                locally so the wire shape is never edited.

        Returns:
            EventRow dicts (id, name, date_text, start_timestamp,
            is_past, is_viewer_host, going count if present) — at most
            ``limit`` of them.
        """
        entry = self._find(self._preloads(EVENTS_URL), LIST_QUERY)
        if entry is not None:
            doc_id, variables = entry.doc_id, dict(entry.variables)
        else:
            doc_id, variables = self.doc_id(LIST_QUERY), dict(DEFAULT_EVENTS_VARIABLES)
        payload = self.client.call(LIST_QUERY, doc_id, variables)
        rows = parse_event_rows(payload)[:limit]
        return [r.model_dump() for r in rows]

    def feed(self, event_id: str, *, limit: int = 10,
             cursor: str | None = None) -> dict[str, Any]:
        """One page of an event's discussion feed
        (EventCometPermalinkDiscussionFeedPaginationQuery).

        The template is the schema-decoded LocalArgument set; ``id`` gets
        the Relay node form (event_node_id), ``count``/``cursor`` the page
        parameters. Stories reuse the feed-surface walker (docs/04 §5):
        every Story node plus the connection page_info cursor.

        Args:
            event_id: The event's numeric fbid or Relay node id.
            limit: The page size; rides the ``count`` variable.
            cursor: Optional opaque pagination token from the previous
                page's end_cursor.

        Returns:
            An EventFeedPage dict: {stories, count, end_cursor,
            has_next_page, raw_size}.
        """
        variables = copy.deepcopy(DEFAULT_EVENT_FEED_VARIABLES)
        variables["id"] = event_node_id(event_id)
        variables["count"] = limit
        variables["cursor"] = cursor
        payload = self.client.call(FEED_QUERY, self.doc_id(FEED_QUERY), variables)
        page = parse_feed_page(payload)
        return EventFeedPage(
            stories=[s.model_dump() for s in page.stories],
            end_cursor=page.end_cursor,
            has_next_page=page.has_next_page,
            raw_size=page.raw_size,
        ).model_dump()

    # ------------------------------------------------------------------ writes
    def delete(self, event_id: str) -> dict[str, Any]:
        """Delete an event the viewer hosts (useEventCometDeleteMutation).

        Schema-decoded 2026-09-18, NO live fire: variables are the decoded
        hook shape ({input: {acontext, event_id}, scale}) with the event id
        substituted. The schema carries no client_mutation_id — none is
        invented. Own events only — the server rejects deletes of
        non-hosted events.

        Args:
            event_id: The numeric fbid of the event to cancel; rides
                input.event_id.

        Returns:
            The merged response carrying
            ``data.event_cancel.canceled_event_id``.
        """
        variables = copy.deepcopy(DELETE_VARIABLES_TEMPLATE)
        variables["input"]["event_id"] = event_id
        return self.client.call(DELETE_MUTATION, self.doc_id(DELETE_MUTATION),
                                variables)

    def create(self, name: str, start_date: str, start_time: str, *,
               end_date: str | None = None, end_time: str | None = None,
               timezone: str = "UTC",
               description: str | None = None,
               privacy: str = "PUBLIC_TYPE") -> dict[str, Any]:
        """Create an event (EventCometLightweightCreateMutation).

        The mutation was DISCOVERED via the /events/create/ delta-harvest
        (2026-09-18); the input is the schema-decoded committer template
        with the CLI-substitutable fields applied. Schema carries no
        client_mutation_id — none is invented. NOT live-fired during
        discovery (docs/11 §2: live execution is the operator's call).

        Args:
            name: The event name.
            start_date: The start date, YYYY-MM-DD.
            start_time: The start time, HH:MM.
            end_date: Optional end date, YYYY-MM-DD.
            end_time: Optional end time, HH:MM.
            timezone: IANA timezone name anchoring the local times.
            description: Optional event description.
            privacy: "PUBLIC_TYPE" or "PRIVATE_TYPE" (the decoded
                committer's two non-group enum literals).

        Returns:
            The merged response carrying ``data.fb_event_create.event.id``.

        Raises:
            ValueError: If ``privacy`` is not PUBLIC_TYPE or PRIVATE_TYPE.
        """
        if privacy not in EVENT_PRIVACY_TYPES[:2]:
            raise ValueError(f"privacy must be PUBLIC_TYPE or PRIVATE_TYPE, "
                             f"got {privacy!r}")
        variables = {"input": copy.deepcopy(CREATE_INPUT_TEMPLATE)}
        variables["input"]["name"] = name
        variables["input"]["start_date"] = start_date
        variables["input"]["start_time"] = start_time
        variables["input"]["end_date"] = end_date
        variables["input"]["end_time"] = end_time
        variables["input"]["timezone"] = timezone
        variables["input"]["description"] = description
        variables["input"]["event_privacy_type"] = privacy
        return self.client.call(CREATE_MUTATION, self.doc_id(CREATE_MUTATION),
                                variables)
