"""Messenger surface service (docs/06 + docs/15).

Threads, history, plaintext send, and realtime listen for the Messenger
surface, grounded in the live-calibration findings and the fresh bundle
archaeology performed for this surface (docs/15 §P2-4/P3-1/P7, docs/13 §2).

ARCHITECTURE:

  This service owns the surface-level contracts, not the codec internals:
  MQIsdp framing, the Thrift-compact reader and the DGW socket machinery
  live in ``realtime/`` (docs/06), and ``listen``/``listen_dgw`` merely
  orchestrate those clients with the session's cookies and deadlines. The
  read/send legs (``threads``/``history``/``send``) ride the ordinary
  GraphQL persisted-query plane through ``Session``/``GraphQLClient``, so
  every call is governor-paced (docs/10 §7) and fully testable against
  ``tests/fakes.py`` stubs. Reads are replay-first with honest degradation:
  the thread list replays the QP contract query and falls back to the
  Lightspeed state bridge; history maps the E2EE protocol gate to an empty
  row set; send refuses non-self targets because the wire-verified plane
  does not reach them (docs/15 §P7).

CALIBRATION NOTES:

  * Thread list — ``MWCMBlendedThreadListQuery`` (doc_id 24319446437729096) is
    the thread-list page's QP query. The real web client preloads it with EMPTY
    variables (``MWCMBlendedThreadList.entrypoint`` -> ``variables:{}``; the
    bundled Operation declares no ``LocalArgument`` at all) and the thread rows
    themselves live in the Lightspeed state store: the inbox is synchronized
    through ``LSPlatformGraphQLLightspeedRequestQuery`` (doc_id 9697184873702141),
    the same bridge that rides the DGW lightspeed socket (docs/15 §P2-4b).
    :meth:`MessengerService.threads` replays the contract query first and, when
    the current build returns only ``eligible_promotions`` (the live-observed
    behavior), falls back to a fresh e2ee thread-snapshot replay (database 95,
    live-verified end to end 2026-09).

  * Send — ``useCometAIHTSSendMessageMutation`` (doc_id 28137996599166900),
    variable schema discovered live by iterating the server's 1675012
    variable-coercion errors: the mutation takes ``{connections, input}`` where
    ``input`` is a strict object — extra fields are rejected with
    ``noncoercible_variable_value`` — whose working shape (verified live) is
    ``{article_cms_id, capability_token, card_action_content_id,
    card_action_content_type, card_action_signal_type, file_attachments,
    is_hidden, message (plain string, NOT ``{ranges,text}``), message_id (fresh
    uuid per call), messaging_user_id, messaging_user_token, qpl_join_id,
    routine_type_override, thread_id}``. A self-addressed send (thread_id ==
    c_user) lands in the account's own chat only. The V2 variant
    (``useCometAIHTSSendMessageV2Mutation``, doc_id 38081592568123136, field
    ``xfb_conversational_support_send_message``) is the fallback when the
    primary mutation is rejected at the protocol level.

  * History — ``EBMessageRangeQueryForThreadsQuery`` (doc_id 27443670391974737,
    registry-backed, owner bundle still live in the 2026-09 deploy) is the
    encrypted-backup (EB) message-range read the web client replays for thread
    history. The variable schema is decoded from the owning bundle's Operation +
    ``EBMessageRangeQueryForThreadsQueryVariables`` builder: ``{app_id,
    includeAttachmentData, restore_payload_strings, restore_type}`` where each
    ``restore_payload_strings`` entry is one JSON range descriptor naming the
    thread (``act_thread_id`` + Int64 ``server_thread_key``), a BEFORE/AFTER
    direction, a reference timestamp, ``query_num_messages`` and an e2ee
     ``device_context``. Live finding (2026-09, probe thread
     12345678901234560 — synthetic in-tree; the real thread id is
     operator-local — six
    name-guided attempts): the coercion layer accepts the shape outright (no
    1675012), but the ``raw_tokens`` are genuine per-device enrollment crypto
    (mailbox_root_key / ocmf_client_state_blob) — without them the endpoint
    answers one deterministic ``field_exception`` (mid b9dead6ed672dfbc0d…)
    with an EMPTY ``messages_from_selected_threads`` row set, and the rows it
    does return for enrolled devices are encrypted stanzas (sk_ciphertext /
    encrypted_protobuf + protobuf_timestamp_ms, sender_user_fbid) — no
    plaintext text field exists in the Operation. :meth:`history` therefore
    maps the protocol-level rejection to an empty history and walks whatever
    message-shaped nodes the build returns.

  * Realtime — the edge-chat MQTT bus over WebSocket (docs/06 §3-4, the
    MQIsdp/level-3 dialect confirmed live in docs/15 §P3-1): CONNECT ->
    CONNACK rc=0 -> SUBSCRIBE ``/t_ms`` + ``/t_rtc_multi`` -> PUBLISH deltas
    with Thrift-compact bodies. The modern web typing indicator rides the
    Lightspeed/DGW bridge (``sendChatStateFromComposer``), not a ``$typ`` MQTT
    publish — no publish shape exists in the shipped bundles, so this service
    deliberately exposes no typing method.

USER-DOC ANCHOR: cli/docs/06-reference-people.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import base64
import contextlib
import json
import random
import time
import uuid
from collections.abc import Iterator
from typing import Any

import constants as C
from domain.common import Actor, Message, ThreadSummary
from graphql.errors import GraphQLProtocolError
from realtime.dgw.requests import LightspeedClient
from realtime.mqtt.client import MQTTClient

from .base import Surface

# --------------------------------------------------------------------------
# Contract constants (registry names + live-discovered variable schemas).
# --------------------------------------------------------------------------

#: Friendly name of the thread-list query (registry: 24319446437729096).
THREADLIST_QUERY = "MWCMBlendedThreadListQuery"

#: Friendly name of the Lightspeed state bridge (registry: 9697184873702141).
LIGHTSPEED_QUERY = "LSPlatformGraphQLLightspeedRequestQuery"

#: Primary send mutation (registry: 28137996599166900).
SEND_MUTATION = "useCometAIHTSSendMessageMutation"

#: Fallback send mutation, V2 variant (registry: 38081592568123136).
SEND_MUTATION_V2 = "useCometAIHTSSendMessageV2Mutation"

#: The live-verified thread-list variables: the bundled entrypoint preloads
#: this query with an EMPTY variable set and the persisted document declares
#: no arguments at all (docs/15 §P2-4; MWCMBlendedThreadList.entrypoint).
DEFAULT_THREADLIST_VARIABLES: dict[str, Any] = {}

#: Lightspeed e2ee thread-snapshot request template (live-verified against
#: the /messages/ preloads + DGW capture, docs/15 §P2-4). ``version`` is the
#: LS schema revision — re-harvest it from a fresh /messages/ preload when it
#: rolls (scripts/02 / 21 methodology, docs/13 §2).
LIGHTSPEED_SNAPSHOT_REQUEST: dict[str, Any] = {
    "database": 95,
    "epoch_id": 0,
    "last_applied_cursor": None,
    "sync_params": "{\"locale\":\"en_GB\"}",
    "version": 28803601955930286,
}

#: useCometAIHTSSendMessageMutation input template — the exact field set the
#: web client sends (bundle ``useCometAIHTSSendMessage``), verified live;
#: ``message``, ``message_id`` and ``thread_id`` are substituted per call.
DEFAULT_SEND_INPUT: dict[str, Any] = {
    "article_cms_id": None,
    "capability_token": None,
    "card_action_content_id": None,
    "card_action_content_type": None,
    "card_action_signal_type": None,
    "file_attachments": None,
    "is_hidden": False,
    "message": "",
    "message_id": "",
    "messaging_user_id": None,
    "messaging_user_token": None,
    "qpl_join_id": None,
    "routine_type_override": None,
    "thread_id": "",
}

#: V2 send input — same discovery path, minus the V1-only token/card fields
#: (bundle call site: article_cms_id, is_hidden, message, message_id,
#: messaging_user_id, messaging_user_token, qpl_join_id, thread_id).
DEFAULT_SEND_INPUT_V2: dict[str, Any] = {
    "article_cms_id": None,
    "is_hidden": False,
    "message": "",
    "message_id": "",
    "messaging_user_id": None,
    "messaging_user_token": None,
    "qpl_join_id": None,
    "thread_id": "",
}

#: Response fields whose presence marks a successful send (one per variant).
_SEND_RESPONSE_FIELDS = ("xfb_comet_ai_hts_send_message_mutation",
                         "xfb_conversational_support_send_message")


#: The thread id present in the live DGW lightspeed capture (docs/15
#: §P3-7) — the default subscribe target when listen_dgw gets none.
#: Synthetic in-tree; the real probe thread id is operator-local.
DGW_DEFAULT_THREAD_ID = "12345678901234560"

#: Friendly name of the encrypted-backup message-range query the web client
#: replays for thread history (registry: 27443670391974737 — the owner
#: bundle's registration was still live in the 2026-09 /messages/ deploy).
HISTORY_QUERY = "EBMessageRangeQueryForThreadsQuery"

#: Range direction constants (LSEncryptedBackupsMessagesRangeQueryDirection
#: in the owning bundle): 1 = BEFORE — messages older than the reference.
HISTORY_DIRECTION_BEFORE = 1

#: History query defaults # live-probed 2026-09 (six name-guided attempts
#: against the probe thread (synthetic in-tree); the coercion layer accepted
#: every
#: variant — no 1675012 ever fired). ``app_id`` is the messenger web app id
#: from the live DGW envelope; ``restore_payload_strings`` is rebuilt per
#: call by :func:`_history_range_payload`; ``restore_type`` distinguishes
#: first-sync restores from windowed range queries.
DEFAULT_HISTORY_VARIABLES: dict[str, Any] = {
    "app_id": "2220391788200892",
    "includeAttachmentData": False,
    "restore_payload_strings": [],
    "restore_type": "RANGE_QUERY_RESTORE",
}


def _history_thread_key(thread_id: str) -> str:
    """Normalize a thread id to the numeric thread key the EB range query
    keys on.

    Raw numeric keys pass through; the b64 global (Relay) id form —
    base64 of ``"Thread:<n>"`` — decodes to ``<n>``, the same
    normalization the events surface applies to ``"Event:<n>"`` ids
    (docs/15 P4-4). Anything else is returned unchanged (opaque keys are
    the server's business, not ours).
    """
    key = thread_id.strip()
    if key.isdigit():
        return key
    if key.startswith("Thread:"):
        key = key.split(":", 1)[1]
        return key if key.isdigit() else thread_id
    try:
        decoded = base64.b64decode(
            key + "=" * (-len(key) % 4)).decode("utf-8", "replace")
    except (ValueError, TypeError):
        return thread_id
    if decoded.startswith("Thread:"):
        inner = decoded.split(":", 1)[1]
        if inner.isdigit():
            return inner
    return thread_id


def _history_range_payload(thread_key: str, limit: int, *,
                           now_ms: int | None = None,
                           direction: int = HISTORY_DIRECTION_BEFORE) -> str:
    """Build one JSON range descriptor for ``restore_payload_strings``.

    Mirrors the bundle's ``EBMessageRangeQueryForThreadsQueryVariables``
    builder verbatim: ``restore_context`` names the thread (act_thread_id
    plus its Int64 server_thread_key — the client's own
    threadIdToThreadIdInt helper demands digits), ``success`` carries the
    device context, the direction, the reference timestamp (now -> the
    most recent window) and ``query_num_messages`` — the query-side limit.
    A fresh uuid device_id was accepted live; the ``raw_tokens`` are
    per-device e2ee enrollment state that cannot be fabricated (see
    DEFAULT_HISTORY_VARIABLES for the live finding).
    """
    reference = now_ms if now_ms is not None else int(time.time() * 1000)
    server_thread_key: int | str = (
        int(thread_key) if thread_key.isdigit() else thread_key)
    return json.dumps({
        "restore_context": {
            "act_thread_id": thread_key,
            "client_mek_fbids": [],
            "site": "www",
            "source": 0,
            "tam_thread_subtype": 0,
        },
        "success": {
            "device_context": {
                "device_id": str(uuid.uuid4()),
                "locally_available_epochs": [],
                "raw_tokens": {
                    "mailbox_root_key": base64.b64encode(b"\x00" * 32).decode(),
                    "ocmf_client_state_blob": base64.b64encode(b"\x00" * 16).decode(),
                },
            },
            "direction": direction,
            "query_num_messages": limit,
            "reference_timestamp": str(reference),
            "server_thread_key": server_thread_key,
            "source": 0,
        },
    }, separators=(",", ":"))


class MessengerService(Surface):
    """The Messenger surface: threads, history, send, realtime listen.

    Encapsulates the docs/06 message plane as live-calibrated 2026-09: the
    GraphQL read/send legs plus orchestration of the realtime/ MQTT and DGW
    clients for ``listen``/``listen_dgw``. The classic ``/t_ms`` bus is dead
    for chats (docs/15 §P7-1) — chat state rides the DGW lightspeed sync —
    so the surface's honest contract is: threads/history read through the
    state bridges, send reaches the self/AI chat only.
    """

    # ------------------------------------------------------------------ threads
    def threads(self, *, variables: dict[str, Any] | None = None,
                limit: int = 20) -> list[ThreadSummary]:
        """List recent message threads, newest first, capped at ``limit``.

        Executes ``MWCMBlendedThreadListQuery`` with
        ``DEFAULT_THREADLIST_VARIABLES`` merged with caller ``variables`` and
        walks the merged payload for thread-shaped nodes (any dict whose
        ``__typename`` contains "Thread" with an id plus name/participants/
        messages). The current build carries the actual thread rows in the
        Lightspeed sync instead (docs/15 §P2-4), so an empty walk falls back
        to replaying the live-verified e2ee thread snapshot through the
        ``LSPlatformGraphQLLightspeedRequestQuery`` bridge.

        Args:
            variables: Caller overrides merged over the live-verified default
                set — the bundled Operation declares no LocalArgument, so
                extra keys risk a 1675012 coercion rejection; pass ``None`` to
                replay the contract verbatim.
            limit: Row cap applied after the walk/fallback.

        Returns:
            ``ThreadSummary`` rows (id, name, snippet, participant ids,
            is_self_thread), newest first; ``[]`` when neither the QP query
            nor the Lightspeed snapshot carries thread rows.

        Raises:
            FBGraphError: Typed session/checkpoint/rate-limit errors from
                the GraphQL client propagate unchanged to the caller.
        """
        merged_vars: dict[str, Any] = dict(DEFAULT_THREADLIST_VARIABLES)
        if variables:
            merged_vars.update(variables)
        response = self.client.call(
            THREADLIST_QUERY, self.doc_id(THREADLIST_QUERY), merged_vars)
        found = list(_walk_thread_nodes(response, self.session.user_id()))
        if not found:
            found = self._lightspeed_threads()
        return found[:limit]

    def _lightspeed_threads(self) -> list[ThreadSummary]:
        """Replay the Lightspeed e2ee thread snapshot (docs/15 §P2-4).

        The bridge query takes the client's own device id (any fresh uuid is
        accepted — verified live), a request id, the LS request envelope as a
        JSON string and the request type (1 = sync transaction).

        Returns:
            Thread summaries parsed from the snapshot's
            ``updateOrInsertThread`` rows; ``[]`` when the response carries
            no parseable lightspeed payload.
        """
        variables = {
            "deviceId": str(uuid.uuid4()),
            "requestId": random.getrandbits(16),
            "requestPayload": json.dumps(LIGHTSPEED_SNAPSHOT_REQUEST,
                                         separators=(",", ":")),
            "requestType": 1,
        }
        response = self.client.call(
            LIGHTSPEED_QUERY, self.doc_id(LIGHTSPEED_QUERY), variables)
        return list(_lightspeed_thread_summaries(response, self.session.user_id()))

    # ------------------------------------------------------------------ history
    def history(self, thread_id: str, *, limit: int = 20) -> list[Message]:
        """Read one thread's recent messages, newest first, capped at ``limit``.

        Executes ``EBMessageRangeQueryForThreadsQuery`` with the live-probed
        default variables: the thread id is normalized to the numeric thread
        key (raw digits pass through; the b64 ``"Thread:<n>"`` global id
        decodes to ``<n>``) and substituted into one
        ``restore_payload_strings`` range descriptor; ``limit`` rides the
        query itself (``query_num_messages``), with a post-slice as belt and
        braces. The merged payload is walked for message-shaped nodes and
        typed to ``Message`` (sender Actor, text, timestamp ms) — the EB
        rows carry stanza-level fields (sender_user_fbid,
        protobuf_timestamp_ms, otid; text stays None for encrypted stanzas)
        while legacy/plaintext message nodes (sender/text/timestamp) type
        through unchanged.

        Live finding (2026-09): without the device's real e2ee enrollment
        tokens the endpoint answers one deterministic ``field_exception``
        with an EMPTY row set, so a protocol-level rejection maps to an
        empty history; typed session/checkpoint/rate errors still propagate.

        Args:
            thread_id: The thread to read — a raw numeric key, a ``"Thread:<n>"``
                global id, or the b64 ``"Thread:<n>"`` Relay form (normalized
                by :func:`_history_thread_key`).
            limit: Row cap; rides the wire as ``query_num_messages`` AND is
                re-applied as a post-slice.

        Returns:
            ``Message`` rows (sender Actor, text, timestamp ms) newest
            first; encrypted EB stanzas type through with ``text=None``
            (no plaintext field exists in the Operation — docs/15 §P7-2).
            ``[]`` on the E2EE protocol gate or a genuinely empty thread.

        Raises:
            FBGraphError: Typed session/checkpoint/rate-limit errors
                propagate unchanged (only the protocol-level
                ``GraphQLProtocolError`` maps to the empty history).
        """
        key = _history_thread_key(thread_id)
        variables = dict(DEFAULT_HISTORY_VARIABLES)
        variables["restore_payload_strings"] = [_history_range_payload(key, limit)]
        try:
            response = self.client.call(
                HISTORY_QUERY, self.doc_id(HISTORY_QUERY), variables)
        except GraphQLProtocolError:
            return []
        found = list(_walk_message_nodes(response, key))
        found.sort(key=lambda m: m.timestamp_ms or 0, reverse=True)
        return found[:limit]

    # -------------------------------------------------------------------- send
    class NonSelfThreadError(GraphQLProtocolError):
        """Refusing to send: the only wire-verified send plane is the
        AI/self chat (docs/15 §P7) — person-to-person threads are E2EE and
        route through the client-side crypto pipeline, so plaintext sends
        to them silently misroute. Pass force=True to override knowingly."""

    def send(self, thread_id: str, text: str, *, force: bool = False) -> dict[str, Any]:
        """Send one text message; return the merged response.

        LIVE-VERIFIED SCOPE (docs/15 §P7): the AIHTS mutation delivers only
        to the self/AI chat. Sending to a person-to-person thread requires
        the E2EE client pipeline (per-device MEKs + wasm crypto feeding the
        Lightspeed job queue) and CANNOT be done over the plaintext wire —
        attempts silently no-op (verified: thread snippet unchanged). This
        method therefore refuses non-self targets unless ``force=True``.

        ``message_id`` is fresh per call (uuid4 — the per-send id the web
        client generates). On a protocol-level rejection of the primary
        mutation the V2 variant is tried once; typed session/checkpoint/rate
        errors propagate unchanged to the caller.

        Args:
            thread_id: Target thread; the only non-refused target is the
                session's own user id (the self/AI chat) unless ``force``.
            text: Message body — a plain string on the wire, NOT the
                ``{ranges, text}`` object other composer mutations use.
            force: Knowingly override the E2EE self-thread guard (protocol
                experiments only — docs/15 §P7-3 silent-misroute finding).

        Returns:
            The merged GraphQL response — check delivery with
            :meth:`sent_ok` (message rows present), never with the success
            envelope alone (soft suppression is invisible in-band).

        Raises:
            NonSelfThreadError: When ``thread_id`` differs from the viewer
                and ``force`` is false — the send was refused before any
                network I/O.
            FBGraphError: Typed session/checkpoint/rate-limit errors from
                either mutation attempt propagate unchanged.
        """
        if not force and thread_id != self.session.user_id():
            raise self.NonSelfThreadError(
                f"refusing plaintext send to non-self thread {thread_id!r}: "
                "P2P messages are E2EE (docs/15 §P7) and plaintext attempts "
                "silently misroute; use --force only for protocol experiments")
        primary_input = dict(DEFAULT_SEND_INPUT)
        primary_input.update({
            "message": text,
            "message_id": str(uuid.uuid4()),
            "thread_id": thread_id,
        })
        try:
            return self.client.call(
                SEND_MUTATION, self.doc_id(SEND_MUTATION), {"input": primary_input})
        except GraphQLProtocolError:
            v2_input = dict(DEFAULT_SEND_INPUT_V2)
            v2_input.update({
                "message": text,
                "message_id": str(uuid.uuid4()),
                "thread_id": thread_id,
            })
            return self.client.call(
                SEND_MUTATION_V2, self.doc_id(SEND_MUTATION_V2),
                {"input": v2_input})

    @staticmethod
    def sent_ok(response: dict[str, Any]) -> bool:
        """Whether a send response actually carries message rows.

        Distinguishes a real delivery echo from a success-shaped envelope
        (docs/10 §3.4 soft-suppression): the response must carry one of the
        live-observed per-variant mutation fields with a truthy value.

        Args:
            response: The merged GraphQL response from :meth:`send`.

        Returns:
            True only when a variant's response field is present and
            non-empty; False for empty/failed sends and non-dict payloads.
        """
        data = response.get("data")
        if not isinstance(data, dict):
            return False
        return any(
            field in data and data[field] for field in _SEND_RESPONSE_FIELDS)

    # ------------------------------------------------------------------ listen
    def listen(self, topics: list[str] | None = None, *,
               seconds: float = 30.0) -> list[dict[str, Any]]:
        """Subscribe to MQTT topics and collect PUBLISH frames for ``seconds``.

        Connects the edge-chat MQIsdp broker with the session cookies
        (docs/06 §3, docs/15 §P3-1), subscribes each topic with packet ids
        1..n, then pumps ``read()`` until the monotonic deadline — keepalive
        pings fire inside ``read()`` on idle. Defaults to the live-observed
        web-client subscribe set (``/t_ms`` + ``/t_rtc_multi``).

        Args:
            topics: Subscribe targets; ``None`` replays the live-observed
                web-client set. ``/t_ms`` is the only universally confirmed
                delta topic (docs/06 §4 topic map).
            seconds: Collection window; the socket is closed at the
                monotonic deadline regardless of pending frames.

        Returns:
            One ``{"topic", "kind", "size", "summary"}`` dict per received
            PUBLISH frame — payload bodies are Thrift-compact (docs/06 §5)
            and are summarized, not decoded.

        Raises:
            Whatever ``MQTTClient.connect`` raises on a failed WS
                upgrade/handshake (transport-layer, not typed GraphQL
                errors) — a rejected CONNECT is fatal for the window.
        """
        targets = list(topics) if topics else [
            C.MQTT_TOPICS["main_sync"], C.MQTT_TOPICS["rtc_multi"]]
        collected: list[dict[str, Any]] = []
        client = MQTTClient(self.session.cookies, url=C.MESSENGER_MQTT_WS)
        try:
            client.connect(self.session.user_id())
            for packet_id, topic in enumerate(targets, start=1):
                client.subscribe(topic, packet_id=packet_id)
            deadline = time.monotonic() + max(0.0, seconds)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                frame = client.read(timeout=min(5.0, remaining))
                if frame is None:
                    continue
                if frame.kind == "PUBLISH":
                    collected.append({
                        "topic": frame.topic,
                        "kind": frame.kind,
                        "size": len(frame.payload),
                        "summary": frame.thrift_summary(),
                    })
        finally:
            # Teardown must never mask the loop's original exception: a
            # partially-connected socket (connect() failed mid-handshake)
            # can raise a secondary error out of close().
            with contextlib.suppress(Exception):
                client.close()
        return collected

    def listen_dgw(self, *, channel: str = "lightspeed", seconds: float = 30.0,
                   thread_ids: list[str] | None = None) -> list[dict[str, Any]]:
        """Subscribe over the DGW lightspeed socket and collect responses.

        Opens a LightspeedClient with the session cookies (docs/15 §P3-7),
        connects with a fresh uuid device id, sends the captured
        client_subscribe for the thread list plus the given thread ids
        (defaulting to the real thread id from the capture), then reads
        typed responses until the deadline — received data frames are
        auto-acked with their ACK8.

        Args:
            channel: DGW gateway channel (docs/15 §P2-4b: ``rpsignaling``,
                ``realtime``, ``lightspeed``, ``streamcontroller``); the
                default ``lightspeed`` is the channel that carries the
                state-sync request/response dialect.
            seconds: Collection window; the socket closes at the deadline.
            thread_ids: Thread keys added to the client_subscribe task
                queue; ``None`` subscribes only the live-captured default
                thread.

        Returns:
            ``[{"request_id": ..., "payload_type": ..., "payload": <json
            head>}]`` — one entry per typed response, payloads trimmed to a
            120-char JSON head for human/journal consumption.

        Raises:
            Whatever ``LightspeedClient.connect`` raises on a failed
                handshake — the DGW server-first ``0x0A`` byte and control
                acks must succeed before any data frames flow.
        """
        targets = list(thread_ids) if thread_ids else [DGW_DEFAULT_THREAD_ID]
        collected: list[dict[str, Any]] = []
        client = LightspeedClient(self.session.cookies, channel)
        try:
            device_id = str(uuid.uuid4())
            client.connect(self.session.user_id(), device_id=device_id)
            client.subscribe_threads(device_id, targets, request_id=1)
            deadline = time.monotonic() + max(0.0, seconds)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                response = client.read_response(timeout=min(5.0, remaining))
                if response is None:
                    continue
                collected.append({
                    "request_id": response.request_id,
                    "payload_type": response.payload_type,
                    "payload": _dgw_payload_head(response.payload),
                })
        finally:
            # Same teardown discipline as listen(): close() is best-effort
            # cleanup and must not mask the loop's original exception.
            with contextlib.suppress(Exception):
                client.close()
        return collected


# --------------------------------------------------------------------------
# Pure response-walking helpers (unit-testable without any network).
# --------------------------------------------------------------------------


def _dgw_payload_head(payload: Any, size: int = 120) -> str:
    """Compact JSON head of a decoded DGW payload (sanitized, for humans)."""
    try:
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return ""
    return text[:size] + ("…" if len(text) > size else "")


def _thread_snippet(node: dict[str, Any]) -> str | None:
    """Best-effort latest-message snippet from a thread-shaped GraphQL node."""
    last = node.get("last_message")
    if isinstance(last, dict):
        message = last.get("message")
        if isinstance(message, dict):
            text = message.get("text")
            if isinstance(text, str):
                return text
        snippet = last.get("snippet")
        if isinstance(snippet, str):
            return snippet
    messages = node.get("messages")
    if isinstance(messages, dict) and isinstance(messages.get("nodes"), list):
        for candidate in reversed(messages["nodes"]):
            if not isinstance(candidate, dict):
                continue
            for key in ("snippet", "text"):
                value = candidate.get(key)
                if isinstance(value, str):
                    return value
    direct = node.get("snippet")
    if isinstance(direct, str):
        return direct
    return None


def _thread_participants(node: dict[str, Any]) -> list[str]:
    """Participant ids from a thread node's participants edges/nodes."""
    ids: list[str] = []
    for key in ("participants", "all_participants"):
        field = node.get(key)
        if isinstance(field, dict) and isinstance(field.get("nodes"), list):
            for entry in field["nodes"]:
                if isinstance(entry, dict) and entry.get("id") is not None:
                    ids.append(str(entry["id"]))
        elif isinstance(field, list):
            for entry in field:
                if isinstance(entry, dict) and entry.get("id") is not None:
                    ids.append(str(entry["id"]))
    return ids


def _looks_like_thread(node: dict[str, Any]) -> bool:
    """The thread-shape predicate: __typename mentions Thread + id + payload."""
    typename = node.get("__typename")
    if not isinstance(typename, str) or "Thread" not in typename:
        return False
    if not node.get("id"):
        return False
    has_payload = bool(
        node.get("name") is not None
        or _thread_participants(node)
        or node.get("messages") is not None
        or node.get("participants") is not None
    )
    return has_payload


def _walk_thread_nodes(payload: Any, self_uid: str) -> Iterator[ThreadSummary]:
    """Recursively walk any merged GraphQL payload for thread-shaped nodes."""
    if isinstance(payload, dict):
        if _looks_like_thread(payload):
            participants = _thread_participants(payload)
            name = payload.get("name")
            yield ThreadSummary(
                id=str(payload["id"]),
                name=str(name) if isinstance(name, str) else None,
                snippet=_thread_snippet(payload),
                participants=participants,
                is_self_thread=participants == [self_uid],
            )
            return
        for value in payload.values():
            yield from _walk_thread_nodes(value, self_uid)
    elif isinstance(payload, list):
        for value in payload:
            yield from _walk_thread_nodes(value, self_uid)


def _message_timestamp_ms(node: dict[str, Any]) -> int | None:
    """Timestamp ms from any live-observed message field (exact-key set:
    the EB stanza field protobuf_timestamp_ms plus the legacy timestamp
    fields — thread rows and range metadata use other names and never
    match, which keeps the walker from mistaking envelopes for rows)."""
    for key in ("protobuf_timestamp_ms", "timestamp_ms", "timestamp"):
        value = node.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def _message_sender(node: dict[str, Any]) -> Actor | None:
    """Sender as an Actor from either live-observed shape: a nested
    ``sender`` node (legacy plaintext rows) or the EB stanza's flat
    ``sender_user_fbid``."""
    sender = node.get("sender")
    if isinstance(sender, dict) and sender.get("id") is not None:
        fields: dict[str, Any] = {"id": str(sender["id"])}
        if isinstance(sender.get("name"), str):
            fields["name"] = sender["name"]
        if isinstance(sender.get("__typename"), str):
            fields["__typename"] = sender["__typename"]
        return Actor(**fields)
    for key in ("sender_user_fbid", "sender_id", "actor_id"):
        value = node.get(key)
        if isinstance(value, (str, int)) and not isinstance(value, bool) \
                and str(value):
            return Actor(**{"id": str(value)})
    return None


def _looks_like_message(node: dict[str, Any]) -> bool:
    """The message-shape predicate: a timestamp plus any identity payload
    (text, sender or a message id — otid or its supplemental form, the
    field pairing observed in the db95 HISTORY_RESTORE capture). Thread-
    shaped nodes are never messages — the thread walker owns those."""
    if _looks_like_thread(node):
        return False
    if _message_timestamp_ms(node) is None:
        return False
    return bool(
        node.get("text") is not None
        or node.get("snippet") is not None
        or node.get("otid") is not None
        or node.get("supplemental_otid") is not None
        or node.get("id") is not None
        or _message_sender(node) is not None
    )


def _walk_message_nodes(payload: Any, thread_key: str) -> Iterator[Message]:
    """Recursively walk a history payload for message-shaped nodes.

    Covers both live-observed envelopes: EB stanza rows (otid /
    supplemental_otid / sender_user_fbid / protobuf_timestamp_ms — text
    stays None for encrypted stanzas) and legacy plaintext message nodes
    (sender/text/timestamp). Order follows the document; history()
    re-sorts newest-first.
    """
    if isinstance(payload, dict):
        if _looks_like_message(payload):
            message_id = payload.get("id") or payload.get("otid") \
                or payload.get("supplemental_otid") or payload.get("message_id")
            text = payload.get("text")
            if not isinstance(text, str):
                text = payload.get("snippet") \
                    if isinstance(payload.get("snippet"), str) else None
            yield Message(
                id=str(message_id) if message_id is not None else None,
                thread_id=thread_key,
                sender=_message_sender(payload),
                text=text,
                timestamp_ms=_message_timestamp_ms(payload),
            )
            return
        for value in payload.values():
            yield from _walk_message_nodes(value, thread_key)
    elif isinstance(payload, list):
        for value in payload:
            yield from _walk_message_nodes(value, thread_key)


# --------------------------------------------------------------------------
# Lightspeed wire parsing (the LS snapshot payload — docs/15 §P2-4b).
# --------------------------------------------------------------------------

def _ls_find_ops(node: Any, name: str) -> Iterator[list[Any]]:
    """Yield every ``[5, "<name>", <args...>]`` operation in the LS wire.

    The payload is plain JSON; tags used here: ``5`` = string, ``9`` = null,
    ``19`` = Int64-as-string, ``16`` = base64. An operation is the list whose
    first two elements are the literal tag 5 and the operation name.
    """
    if isinstance(node, list):
        if len(node) >= 2 and node[0] == 5 and node[1] == name:
            yield node
        for value in node:
            yield from _ls_find_ops(value, name)
    elif isinstance(node, dict):
        for value in node.values():
            yield from _ls_find_ops(value, name)


def _ls_int64s(op: list[Any]) -> list[str]:
    """Every Int64 token (``[19, "<digits>"]``) in one operation, in order."""
    out: list[str] = []
    for token in op[2:]:
        if isinstance(token, list) and len(token) == 2 and token[0] == 19 \
                and isinstance(token[1], str):
            out.append(token[1])
    return out


def _ls_plain_strings(op: list[Any]) -> list[str]:
    """Bare string tokens in one operation (metadata strings, not [5,...] tags)."""
    return [v for v in op[2:]
            if isinstance(v, str)]


def _ls_snippet(op: list[Any]) -> str | None:
    """The thread's last-message snippet: the first content-bearing bare string
    that is not the folder name, a media-fallback URL or an embedded JSON blob
    (live-verified positional behavior of ``updateOrInsertThread`` rows)."""
    for value in _ls_plain_strings(op):
        if not value or value in ("inbox", "pending", "archived", "other"):
            continue
        if value.startswith(("http", "/messaging/", "{")):
            continue
        return value
    return None


def _lightspeed_thread_summaries(
        response: Any, self_uid: str) -> Iterator[ThreadSummary]:
    """Parse thread rows out of a Lightspeed bridge merged response.

    Uses only stable, live-verified row shapes: participant ids come from
    ``addParticipantIdToGroupThread`` ops, the row's own Int64 tokens contain
    the thread key, contact display names come from ``verifyContactRowExists``
    ops, and the snippet is the first content string in the
    ``updateOrInsertThread`` row (docs/15 §P2-4b, LS version 28803601955930286).
    """
    if not isinstance(response, dict):
        return
    data = response.get("data")
    viewer = data.get("viewer") if isinstance(data, dict) else None
    request = (viewer.get("lightspeed_web_request")
               if isinstance(viewer, dict) else None)
    payload = request.get("payload") if isinstance(request, dict) else None
    if not isinstance(payload, str):
        return
    try:
        wire = json.loads(payload)
    except (ValueError, TypeError):
        return

    participants: dict[str, list[str]] = {}
    for op in _ls_find_ops(wire, "addParticipantIdToGroupThread"):
        ids = _ls_int64s(op)
        if len(ids) >= 2:
            participants.setdefault(ids[0], []).append(ids[1])

    contact_names: dict[str, str] = {}
    for op in _ls_find_ops(wire, "verifyContactRowExists"):
        ids = _ls_int64s(op)
        strings = _ls_plain_strings(op)
        if len(ids) >= 1 and len(strings) >= 2:
            contact_names.setdefault(ids[0], strings[1])

    for op in _ls_find_ops(wire, "updateOrInsertThread"):
        ints = _ls_int64s(op)
        thread_key = next(
            (value for value in ints if value in participants), None)
        if thread_key is None:
            # Fallback for threads with no participant rows in this window:
            # the live row shape is [ts, ts, folder_type, thread_key, ...].
            thread_key = ints[3] if len(ints) >= 4 else None
        if thread_key is None:
            continue
        pids = participants.get(thread_key, [])
        name: str | None = None
        if len(pids) == 2 and self_uid in pids:
            name = contact_names.get(next(p for p in pids if p != self_uid))
        yield ThreadSummary(
            id=thread_key,
            name=name,
            snippet=_ls_snippet(op),
            participants=pids,
            is_self_thread=pids == [self_uid],
        )
