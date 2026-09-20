"""DGW lightspeed request semantics (docs/15 §P3-7 item 2, catalog §P6-2).

Message catalog — decoded offline from assets/messenger_ws_full.json
(the full-byte live capture; lightspeed socket, 171 WS messages):

Request dialect (client -> server, 0x0D data frames):
    payload = b"\\x00\\x80" + compact JSON envelope in the captured key
    order ``{"app_id": <web app id>, "payload": "<inner JSON string>",
    "request_id": N, "type": T}`` where T selects the family:

    ====  =======================  ==========================================
    type  payload_type (catalog)   inner payload shape (a JSON string)
    ====  =======================  ==========================================
      3   client_subscribe         {"epoch_id": <snowflake: unix_ms << 22>,
                                    "tasks": [{"failure_count": null,
                                      "label": "<queue label>",
                                      "payload": "<task JSON string>",
                                      "queue_name": "<queue>",
                                      "task_id": T}],
                                    "version_id": "28803601955930286"}
      1   state_sync               {"database": D, "epoch_id": I,
                                    "failure_count": null,
                                    "last_applied_cursor": null,
                                    "sync_params": "<json>|null",
                                    "version": 28803601955930286}
      2   state_sync_delta         same shape as type 1, cursor non-null
    ====  =======================  ==========================================

    Threads are requested by id inside type-3 tasks: the thread-list task
    rides queue ``"trq"`` (label "145", sync_group 1 — the 876B frame that
    opens the capture); per-thread tasks carry
    ``{"thread_key": <id>, "sync_group": 95}``` payloads.

Response dialect (server -> client, 0x0D data frames):
    payload = b"\\x00\\x80" + JSON
    ``{"request_id": N|null, "payload": "<inner JSON string>",
    "sp": [<executed lightspeed op names>], "target": 3}``.

Correlation rule: responses echo the request's ``request_id`` (verified
rid 4<->4, 9<->9, 25<->25 in the capture; rid 7 round-trip live-confirmed
in §P6-2); ``null`` marks server-initiated pushes. The first byte of the
2-byte payload prefix mirrors the request's as well (0x00 initially,
incrementing per retry round).

The ``{"input":{"client_subscription_id": ...}}`` frames ride the
*streamcontroller* socket, not lightspeed: they register GraphQL
subscriptions (``{"input": {..., "client_subscription_id": "<ms>:<rand>"},
"%options": {"useOSSResponseFormat": true,
"client_has_ods_usecase_counters": true}}``) after 0x0F control frames
declare each ``FBGQLS:*_SUBSCRIBE`` method through ``x-dgw-app-XRSS-*``
headers. decode_request_payload types those as ``client_subscribe`` too.

Transport quirks encoded here (all live-captured):
    * one WS message may CONCATENATE several DGW packets (split_ws_frame)
    * 0x0E ACK3 frames are 3 bytes — op + seq, no header
    * 0x0C ACK8 acks carry a 2-byte ``\\x00\\x00`` payload

ARCHITECTURE:

  Typed Envelope Layer over the Socket Layer:
    ``LightspeedClient`` extends ``DGWClient`` (socket/handshake
    mechanics, docs/15 §P3-6) with the lightspeed dialect: request
    envelopes typed by the catalog above, response correlation by
    request_id, and the client-side ACK8 mirroring the captured client
    performs on every received data frame. Pure decode helpers
    (``split_ws_frame``, ``decode_request_payload``) stay side-effect
    free so the offline tests replay the capture through them.

CALIBRATION NOTES (docs/15 §P6-2, live-verified):
    The full catalog was decoded from the capture and the
    client_subscribe/state_sync/state_sync_result round-trips were
    confirmed live (handshake OK, ACK, state_sync_result responses on
    every replayed database — docs/15 §P7-1).

USER-DOC ANCHOR: cli/docs/08-architecture.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import json
import random
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import constants as C

from .client import DGWClient, DGWError
from .frames import DGWFrame

#: Binary prefix on every captured lightspeed 0x0D payload (first byte
#: counts retry rounds, second is constant 0x80).
DATA_FRAME_PREFIX = b"\x00\x80"

#: The 2-byte payload every captured 0x0C ACK8 carries.
ACK8_PAYLOAD = b"\x00\x00"

#: Catalog names for the captured request ``type`` codes.
REQUEST_TYPE_NAMES: dict[int, str] = {
    3: "client_subscribe",   # task-queue request (threads by id)
    1: "state_sync",          # full sync transaction (no cursor)
    2: "state_sync_delta",    # cursor sync transaction
}
REQUEST_TYPE_CODES: dict[str, int] = {v: k for k, v in REQUEST_TYPE_NAMES.items()}

#: Catalog name for the response dialect ({"request_id", "payload",
#: "sp", "target"} frames; target is always 3).
RESPONSE_TYPE_NAME = "state_sync_result"

#: Lightspeed schema revision (docs/15 §P2-4 — same version as the
#: LIGHTSPEED_SNAPSHOT_REQUEST template; type-3 requests carry it as the
#: "version_id" STRING, sync transactions as the "version" int).
LIGHTSPEED_VERSION_ID = "28803601955930286"

#: The captured thread-list task (queue "trq", label "145", sync_group 1 —
#: the first 876B WS message of the lightspeed socket).
THREAD_LIST_QUEUE = "trq"
THREAD_LIST_LABEL = "145"

#: The captured per-thread task shape ({"thread_key": id, "sync_group": 95}
#: payloads — the capture references the probe thread, synthetic in-tree:
#: 12345678901234560).
THREAD_QUEUE = "proactive_warnings_fetch_queue"
THREAD_LABEL = "192"
THREAD_SYNC_GROUP = 95


@dataclass
class DGWRequest:
    """One typed lightspeed request/response.

    ``request_id`` is the client-chosen correlation id on requests and the
    echoed id on responses (None on server-initiated pushes).
    ``payload_type`` is the catalog name; ``payload`` is the decoded inner
    payload dict (the "payload" JSON string of the envelope).
    """

    request_id: int | None  # correlation id; None = server-initiated push
    payload_type: str       # catalog name (client_subscribe / state_sync / ...)
    payload: dict[str, Any]  # decoded inner payload dict


class DGWRequestError(RuntimeError):
    """A data frame is not speakable in the lightspeed dialect."""


# --------------------------------------------------------------------------
# Frame splitting / decoding (pure, offline-testable).
# --------------------------------------------------------------------------

def split_ws_frame(data: bytes) -> list[DGWFrame]:
    """Split one WS message into its DGW frames.

    Captured clients CONCATENATE several DGW packets into one message
    (e.g. 0x0F control {} + 0x0D data in the 876B lightspeed opener), so
    this walks offsets: 0x0E ACK3 frames are 3 bare bytes (op + seq, no
    header), everything else is op(1) seq(2) len(2) flags(1) payload(len).

    Args:
        data: One raw WS binary message.

    Returns:
        Every DGW frame the message contains, in wire order.

    Raises:
        ValueError: If any non-ACK3 segment fails ``DGWFrame.decode``
            (short header or truncated payload — a corrupted message
            fails loudly rather than yielding partial frames).
    """
    frames: list[DGWFrame] = []
    offset = 0
    while offset < len(data):
        if data[offset] == C.DGW_OP_ACK3:
            frames.append(DGWFrame(
                op=C.DGW_OP_ACK3,
                seq=int.from_bytes(data[offset + 1:offset + 3], "little"),
                flags=0))
            offset += 3
            continue
        length = int.from_bytes(data[offset + 3:offset + 5], "little")
        frames.append(DGWFrame.decode(
            data[offset:offset + 6 + length]))
        offset += 6 + length
    return frames


def _envelope_json(frame: DGWFrame) -> dict[str, Any]:
    """The JSON envelope of a 0x0D payload, tolerating the binary prefix.

    Every captured data payload starts with a short binary prefix
    (lightspeed: ``\\x00\\x80``; streamcontroller adds ``\\x2c\\x18`` +
    a length varint and trails ``\\x00\\x00``) — skip to the JSON body.
    """
    body = frame.payload
    start = body.find(b"{")
    candidate = body[start:] if start > 0 else body
    try:
        obj = json.loads(candidate.rstrip(b"\x00").decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise DGWRequestError(
            f"data frame payload is not JSON: {body[:40]!r}") from exc
    if not isinstance(obj, dict):
        raise DGWRequestError(f"data frame payload is not an object: {obj!r}")
    return obj


def _decode_payload_field(value: Any) -> dict[str, Any]:
    """Decode the envelope's "payload" JSON string into a dict."""
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except ValueError:
            return {"raw": value}
        return decoded if isinstance(decoded, dict) else {"value": decoded}
    if isinstance(value, dict):
        return value
    return {}


def decode_request_payload(frame: DGWFrame) -> DGWRequest:
    """Type one 0x0D data frame into a DGWRequest per the catalog.

    Classification order mirrors the capture's three envelope shapes:
    the lightspeed request envelope (``app_id`` key), the response
    dialect (``sp`` key), and the streamcontroller GraphQL-subscription
    registration (``input`` key).

    Args:
        frame: A decoded data frame (op must be DGW_OP_DATA).

    Returns:
        The typed ``DGWRequest``.

    Raises:
        DGWRequestError: If the frame is not a data frame, its payload is
            not a JSON object, its ``type`` code is outside the catalog,
            or its shape matches none of the three known envelopes.
    """
    if frame.op != C.DGW_OP_DATA:
        raise DGWRequestError(f"not a data frame: {frame.kind}")
    obj = _envelope_json(frame)
    if "app_id" in obj:
        # lightspeed request envelope: {"app_id", "payload", "request_id", "type"}
        type_code = obj.get("type")
        payload_type = (REQUEST_TYPE_NAMES.get(type_code)
                        if isinstance(type_code, int) else None)
        if payload_type is None:
            raise DGWRequestError(f"unknown request type: {obj.get('type')!r}")
        return DGWRequest(request_id=obj.get("request_id"),
                          payload_type=payload_type,
                          payload=_decode_payload_field(obj.get("payload")))
    if "sp" in obj:
        # lightspeed response: {"request_id", "payload", "sp", "target"}
        return DGWRequest(request_id=obj.get("request_id"),
                          payload_type=RESPONSE_TYPE_NAME,
                          payload=_decode_payload_field(obj.get("payload")))
    if "input" in obj:
        # streamcontroller GraphQL-subscription registration
        # ({"input": {..., "client_subscription_id": ...}, "%options": {...}})
        return DGWRequest(request_id=None, payload_type="client_subscribe",
                          payload=obj)
    raise DGWRequestError(
        f"unrecognized data payload shape: keys={sorted(obj)}")


def fresh_epoch_id() -> int:
    """A fresh lightspeed epoch id (the per-request identifier the capture
    generates fresh each time): unix_ms << 22 | random bits — the captured
    7506666493080831008 is exactly 1789728756948 << 22 plus jitter.

    Snowflake derivation (docs/15 §P6-2): the epoch id is a 22-bit
    time-shifted value, so the low 22 bits are free client entropy while
    the high bits encode the request timestamp in unix milliseconds.

    Returns:
        A fresh epoch id, unique per call.
    """
    return (int(time.time() * 1000) << 22) | random.getrandbits(22)


# --------------------------------------------------------------------------
# The client dialect.
# --------------------------------------------------------------------------

class LightspeedClient(DGWClient):
    """DGWClient subclass speaking the lightspeed request/response dialect.

    Adds the envelope layer over the base socket: typed requests with
    auto-incrementing correlation ids, captured-shape ACK8 mirroring,
    and response reads that tolerate the captured server traffic the raw
    ``read()`` cannot express (3-byte ACK3s, concatenated packets, the
    stray server-first byte).
    """

    def __init__(self, cookies: Mapping[str, str], channel: str = "lightspeed",
                 *, user_agent: str | None = None,
                 logging_id: str | None = None,
                 open_timeout: float = 15.0):
        """Configure the lightspeed channel session.

        Args:
            cookies: The facebook.com cookie jar for the WS handshake.
            channel: The gateway channel; 'lightspeed' by default.
            user_agent: Optional UA override (sanitized before shipping).
            logging_id: The x-dgw-loggingid uuid; fresh uuid4 when None.
            open_timeout: Per-exchange deadline in seconds.
        """
        super().__init__(cookies, channel, user_agent=user_agent,
                         logging_id=logging_id, open_timeout=open_timeout)
        self.device_id: str | None = None
        self._request_seq = 0

    # ----------------------------------------------------------------- connect
    def connect(self, user_id: str, *, device_id: str | None = None) -> None:
        """Open the socket and complete the {'code':200} handshake.

        Delegates to the base connect() (docs/15 §P3-6): since Phase 10 the
        handshake rides curl_cffi WS with the same chrome TLS impersonation
        and session cookies as the HTTP transport (docs/16 §P10-2), so the
        lightspeed socket keeps a unified fingerprint too. (The old
        websockets-library override here was the last split-identity path.)
        """
        super().connect(user_id, device_id=device_id)

    # ------------------------------------------------------------- requests
    def _next_request_id(self) -> int:
        # Correlation ids count from 1, matching the capture (rid 4 echoes
        # 4, 9 echoes 9 — the server replays the client's id verbatim).
        self._request_seq += 1
        return self._request_seq

    def _send_request_frame(self, payload: bytes) -> DGWFrame:
        """Send one data frame the way the captured client does: preceded by
        a same-seq {} control frame (the ack request) in ONE WS message."""
        self._require_connected()
        if self._ws is None:  # pragma: no cover - guarded by _require_connected
            raise DGWError("not connected — call connect() first")
        seq = self._next_seq()
        control = DGWFrame(op=C.DGW_OP_CONTROL, seq=seq, flags=0, payload=b"{}")
        frame = DGWFrame(op=C.DGW_OP_DATA, seq=seq, flags=0, payload=payload)
        # one WS message, two DGW packets: the control ack-request and the
        # data frame share the sequence number (concatenation quirk, P6-2)
        self._ws.send(control.encode() + frame.encode())
        return frame

    def request(self, payload_type: str, payload: dict[str, Any],
                *, request_id: int | None = None) -> DGWRequest:
        """Build + send one lightspeed request envelope; returns it typed.

        ``request_id`` auto-increments from 1 when not given.

        Args:
            payload_type: A catalog name ('client_subscribe',
                'state_sync', 'state_sync_delta').
            payload: The inner payload dict; JSON-encoded as the
                envelope's "payload" string exactly as the capture does.
            request_id: Optional explicit correlation id (the capture's
                rid 4/9/25 shapes); auto-increments when None.

        Returns:
            The typed ``DGWRequest`` that was sent (correlation-ready).

        Raises:
            DGWRequestError: If ``payload_type`` is outside the catalog.
            DGWError: If not connected.
        """
        type_code = REQUEST_TYPE_CODES.get(payload_type)
        if type_code is None:
            raise DGWRequestError(
                f"unknown payload_type {payload_type!r} "
                f"(known: {sorted(REQUEST_TYPE_CODES)})")
        rid = request_id if request_id is not None else self._next_request_id()
        envelope = {
            "app_id": C.MESSENGER_WEB_APP_ID,
            "payload": json.dumps(payload, separators=(",", ":")),
            "request_id": rid,
            "type": type_code,
        }
        wire = DATA_FRAME_PREFIX + json.dumps(
            envelope, separators=(",", ":")).encode("utf-8")
        self._send_request_frame(wire)
        return DGWRequest(request_id=rid, payload_type=payload_type,
                          payload=payload)

    def subscribe_threads(self, device_id: str, thread_ids: list[str],
                          *, request_id: int = 1) -> DGWRequest:
        """Send the captured client_subscribe for threads (type 3).

        Task shapes straight from the capture (docs/15 §P3-7): one
        thread-list task (queue "trq", label "145", sync_group 1 — the
        same subscribe the captured web client sends for the thread list)
        plus, per thread id, the captured per-thread task
        (queue "proactive_warnings_fetch_queue", label "192", payload
        ``{"thread_key": <id>, "sync_group": 95}``). ``device_id`` is the
        fresh uuid the caller generated (kept on the client for the
        connect URL); ``epoch_id`` is generated fresh per call.
        """
        self.device_id = device_id
        thread_list_task = {
            "failure_count": None,
            "label": THREAD_LIST_LABEL,
            "payload": json.dumps({
                "is_after": 0,
                "parent_thread_key": -1,
                "reference_thread_key": 0,
                "reference_activity_timestamp": 9999999999999,
                "additional_pages_to_fetch": 0,
                "cursor": None,
                "messaging_tag": None,
                "sync_group": 1,
            }, separators=(",", ":")),
            "queue_name": THREAD_LIST_QUEUE,
            "task_id": 0,
        }
        tasks = [thread_list_task]
        for offset, thread_id in enumerate(thread_ids, start=1):
            tasks.append({
                "failure_count": None,
                "label": THREAD_LABEL,
                "payload": json.dumps({
                    "thread_key": int(thread_id),
                    "sync_group": THREAD_SYNC_GROUP,
                }, separators=(",", ":")),
                "queue_name": THREAD_QUEUE,
                "task_id": offset,
            })
        envelope = {
            "epoch_id": fresh_epoch_id(),
            "tasks": tasks,
            "version_id": LIGHTSPEED_VERSION_ID,
        }
        return self.request("client_subscribe", envelope, request_id=request_id)

    # ------------------------------------------------------------ responses
    def _send_ack8(self, frame: DGWFrame) -> None:
        """The matching ACK8 for a received data frame (captured shape:
        op + seq + len=2 + flags=0 + payload ``\\x00\\x00``)."""
        if self._ws is not None:
            self._ws.send(DGWFrame(op=C.DGW_OP_ACK8, seq=frame.seq, flags=0,
                                   payload=ACK8_PAYLOAD).encode())

    def read_response(self, timeout: float = 10.0) -> DGWRequest | None:
        """One typed response, auto-acking data frames; None when idle.

        Handles the captured server traffic the base ``read()`` cannot:
        3-byte ACK3 messages, concatenated DGW packets and the stray
        0x0A server-first byte. A keepalive ping fires on idle timeouts.
        """
        self._require_connected()
        if self._ws is None:  # pragma: no cover - guarded by _require_connected
            raise DGWError("not connected — call connect() first")
        try:
            data = self._ws.recv(timeout=timeout)
        except TimeoutError:
            self.ping()
            return None
        assert isinstance(data, bytes), f"expected binary frame, got {type(data)!r}"
        if data == bytes([C.DGW_SERVER_FIRST_BYTE]):
            return None
        response: DGWRequest | None = None
        for frame in split_ws_frame(data):
            if frame.op != C.DGW_OP_DATA:
                continue
            self._send_ack8(frame)
            response = decode_request_payload(frame)
        return response
