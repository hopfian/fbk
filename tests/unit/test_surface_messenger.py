"""MessengerService unit tests (offline, StubSession) + live-gated realtime.

Canned data comes from the LIVE-captured, sanitized assets fixture
(assets/messenger_threads_fixture.json — the real Lightspeed thread-snapshot
response, message text replaced with "fixture", ids/shapes kept; docs/15).

DGW request tests replay the REAL full-byte capture
(assets/messenger_ws_full.json — docs/15 §P3-7): the lightspeed socket's
876B concatenated opener and its actual request/response frames.
"""
import base64
import json
import sys
import time
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fakes import ASSETS, StubSession

from constants import DGW_OP_ACK3, DGW_OP_CONTROL, DGW_OP_DATA
from domain.common import Message, ThreadSummary
from graphql.errors import GraphQLProtocolError
from realtime.dgw import (
    DGWRequest,
    LightspeedClient,
    decode_request_payload,
    split_ws_frame,
)
from realtime.dgw.frames import DGWFrame
from realtime.dgw.requests import DGWRequestError
from surfaces.messenger import (
    DEFAULT_HISTORY_VARIABLES,
    DEFAULT_SEND_INPUT,
    DEFAULT_SEND_INPUT_V2,
    DEFAULT_THREADLIST_VARIABLES,
    DGW_DEFAULT_THREAD_ID,
    LIGHTSPEED_SNAPSHOT_REQUEST,
    MessengerService,
)

THREADLIST_DOC_ID = "24319446437729096"
LIGHTSPEED_DOC_ID = "9697184873702141"
SEND_DOC_ID = "28137996599166900"
SEND_V2_DOC_ID = "38081592568123136"
HISTORY_DOC_ID = "27443670391974737"

#: Hermetic registry pairs: exactly the five queries the surface touches.
REGISTRY_PAIRS = {
    "MWCMBlendedThreadListQuery": THREADLIST_DOC_ID,
    "LSPlatformGraphQLLightspeedRequestQuery": LIGHTSPEED_DOC_ID,
    "useCometAIHTSSendMessageMutation": SEND_DOC_ID,
    "useCometAIHTSSendMessageV2Mutation": SEND_V2_DOC_ID,
    "EBMessageRangeQueryForThreadsQuery": HISTORY_DOC_ID,
}

#: The REAL live response of MWCMBlendedThreadListQuery with the default
#: (empty) variable set — the current build returns only the promotions
#: envelope; thread rows live in the Lightspeed snapshot (docs/15 §P2-4).
PROMOTIONS_ONLY_RESPONSE = {
    "data": {"viewer": {"eligible_promotions": {"nodes": []}}},
    "extensions": {"is_final": True},
}


def load_fixture() -> dict:
    """The sanitized live Lightspeed snapshot (assets/messenger_threads_fixture.json)."""
    return json.loads(
        (ASSETS / "messenger_threads_fixture.json").read_text(encoding="utf-8"))


def canned_send_response() -> dict:
    """A minimal send echo in the live response shape (docs/15 §P3-style)."""
    return {"data": {"xfb_comet_ai_hts_send_message_mutation": [
        {"id": "resp-1", "messages": [{"id": "m-1", "text": "echo"}]}]}}


def canned_history_response() -> dict:
    """The live-probed EBMessageRangeQueryForThreadsQuery response shape:
    viewer.encrypted_backup.mailbox.messages_from_selected_threads rows
    carrying encrypted stanza messages (field names straight from the owner
    bundle's Operation + the db95 HISTORY_RESTORE capture) plus one legacy
    plaintext node to cover the generic walker path."""
    return {
        "data": {"viewer": {"encrypted_backup": {
            "id": "1570327654890895",
            "mailbox": {"messages_from_selected_threads": [{
                "backup_id": "1570327654890895",
                "message_range_info": {"has_more_before": False,
                                        "has_more_after": False,
                                        "thread_not_found": False},
                "encrypted_messages": [
                    {"otid": "7499890519997198926",
                     "sender_user_fbid": "10000000000000007",
                     "protobuf_timestamp_ms": 1788113248487},
                    {"otid": "7499890551744038973",
                     "sender_user_fbid": "12345678901234",
                     "protobuf_timestamp_ms": 1788113250111},
                ],
                "legacy_message": {
                    "id": "mid_legacy_1",
                     "sender": {"id": "12345678901234561",
                               # scrubbed: real participant name -> synthetic
                               "name": "Person A"},
                    "text": "plain history row",
                    "timestamp_ms": 1788113260000},
            }]}}}},
        "extensions": {"is_final": True},
    }


def make_service(responses: dict) -> tuple[MessengerService, StubSession]:
    session = StubSession(responses, registry_pairs=dict(REGISTRY_PAIRS))
    return MessengerService(session), session


class TestThreads:
    """Pins thread-list semantics: contract query with exact default
    variables first, Lightspeed snapshot fallback, and direct
    GraphQL-node walking on alternate envelopes."""

    def test_threads_from_fixture_via_lightspeed_fallback(self):
        """Threads come from the sanitized LIVE snapshot fixture; the contract
        query is executed first with the exact default variables."""
        service, session = make_service({
            "MWCMBlendedThreadListQuery": PROMOTIONS_ONLY_RESPONSE,
            "LSPlatformGraphQLLightspeedRequestQuery": load_fixture(),
        })
        threads = service.threads(limit=10)

        assert len(threads) >= 1
        thread = threads[0]
        assert isinstance(thread, ThreadSummary)
        # scrubbed: the fixture's thread/participant ids are synthetic
        # (12345678901234560 / 12345678901234561) - the real E2EE ids never
        # ride in committed data.
        assert thread.id == "12345678901234560"
        assert thread.is_self_thread is False
        assert set(thread.participants) == {"12345678901234561", "12345678901234"}
        assert thread.snippet == "fixture"

        friendly, doc_id, variables = session.graphql.calls[0]
        assert friendly == "MWCMBlendedThreadListQuery"
        assert doc_id == THREADLIST_DOC_ID
        assert variables == dict(DEFAULT_THREADLIST_VARIABLES)

        ls_friendly, ls_doc_id, ls_variables = session.graphql.calls[1]
        assert ls_friendly == "LSPlatformGraphQLLightspeedRequestQuery"
        assert ls_doc_id == LIGHTSPEED_DOC_ID
        assert isinstance(ls_variables["deviceId"], str)
        assert ls_variables["requestType"] == 1
        sent = json.loads(ls_variables["requestPayload"])
        assert sent == LIGHTSPEED_SNAPSHOT_REQUEST

    def test_threads_variables_overrides_recorded(self):
        service, session = make_service({
            "MWCMBlendedThreadListQuery": PROMOTIONS_ONLY_RESPONSE,
            "LSPlatformGraphQLLightspeedRequestQuery": load_fixture(),
        })
        service.threads(variables={"scale": 2, "extra_probe": "x"})
        _, doc_id, variables = session.graphql.calls[0]
        assert doc_id == THREADLIST_DOC_ID
        assert variables == {**DEFAULT_THREADLIST_VARIABLES,
                             "scale": 2, "extra_probe": "x"}

    def test_threads_walks_graphql_thread_nodes_directly(self):
        """When the contract query DOES carry thread nodes (older builds /
        alternate envelopes) the walker extracts them without any fallback."""
        canned = {"data": {"viewer": {"message_threads": {"nodes": [
            {"__typename": "MessagingThread", "id": "t_100",
             "name": "Alpha",
             "participants": {"nodes": [{"id": "12345678901234"},
                                        {"id": "10000000000000007"}]}},
            {"__typename": "MessagingThread", "id": "t_200",
             "name": "Self",
             "participants": {"nodes": [{"id": "12345678901234"}]},
             "last_message": {"message": {"text": "hello there"}}},
        ]}}}}
        service, session = make_service({"MWCMBlendedThreadListQuery": canned})
        threads = service.threads()
        assert len(session.graphql.calls) == 1
        assert [t.id for t in threads] == ["t_100", "t_200"]
        assert threads[0].name == "Alpha"
        assert threads[0].is_self_thread is False
        assert threads[1].is_self_thread is True
        assert threads[1].snippet == "hello there"

    def test_threads_limit_applies(self):
        canned = {"data": {"nodes": [
            {"__typename": "MessagingThread", "id": f"t_{i}", "name": f"n{i}",
             "participants": {"nodes": [{"id": "1"}]}}
            for i in range(5)]}}
        service, _ = make_service({"MWCMBlendedThreadListQuery": canned})
        assert [t.id for t in service.threads(limit=2)] == ["t_0", "t_1"]


class TestHistory:
    """Pins the E2EE history contract query: restore-payload assembly
    (fresh device ids, id-form normalization), newest-first typing, and
    the live field_exception -> empty-history mapping."""

    def test_history_executes_contract_query_and_types_messages(self):
        """Rows typed from the live-probed structure; the contract query runs
        with the exact probed variable schema and thread substitution."""
        service, session = make_service(
            {"EBMessageRangeQueryForThreadsQuery": canned_history_response()})
        messages = service.history("12345678901234560")

        assert len(messages) == 3
        assert all(isinstance(m, Message) for m in messages)
        # newest first regardless of document order
        assert [m.timestamp_ms for m in messages] == [
            1788113260000, 1788113250111, 1788113248487]
        assert all(m.thread_id == "12345678901234560" for m in messages)

        assert len(session.graphql.calls) == 1
        friendly, doc_id, variables = session.graphql.calls[0]
        assert friendly == "EBMessageRangeQueryForThreadsQuery"
        assert doc_id == HISTORY_DOC_ID
        assert variables["app_id"] == DEFAULT_HISTORY_VARIABLES["app_id"]
        assert variables["includeAttachmentData"] is False
        assert variables["restore_type"] == "RANGE_QUERY_RESTORE"
        (payload,) = variables["restore_payload_strings"]
        descriptor = json.loads(payload)
        assert descriptor["restore_context"]["act_thread_id"] == "12345678901234560"
        assert descriptor["restore_context"]["site"] == "www"
        success = descriptor["success"]
        assert success["server_thread_key"] == 12345678901234560
        assert success["direction"] == 1
        assert success["query_num_messages"] == 20
        assert success["reference_timestamp"].isdigit()
        device = success["device_context"]
        assert device["locally_available_epochs"] == []
        assert set(device["raw_tokens"]) == {
            "mailbox_root_key", "ocmf_client_state_blob"}

        # typed sender extraction from both observed shapes
        stanza = messages[2]
        assert stanza.id == "7499890519997198926"  # otid is the message id
        assert stanza.sender is not None
        assert stanza.sender.id == "10000000000000007"
        assert stanza.text is None  # encrypted stanza — no plaintext field
        legacy = messages[0]
        assert legacy.sender is not None
        assert legacy.sender.id == "12345678901234561"
        assert legacy.sender.name == "Person A"  # scrubbed real name
        assert legacy.text == "plain history row"

    def test_history_substitutes_raw_and_b64_thread_id_forms(self):
        """Raw numeric keys pass through; b64 "Thread:<n>" global ids (and
        the plain Thread:<n> spelling) decode to the numeric key before
        substitution — the events-surface id normalization (docs/15 P4-4)."""
        b64_form = base64.b64encode(b"Thread:12345678901234560").decode().rstrip("=")
        for form in (b64_form, "Thread:12345678901234560", "12345678901234560"):
            service, session = make_service(
                {"EBMessageRangeQueryForThreadsQuery": canned_history_response()})
            messages = service.history(form)
            assert len(messages) == 3
            _, doc_id, variables = session.graphql.calls[0]
            assert doc_id == HISTORY_DOC_ID
            descriptor = json.loads(variables["restore_payload_strings"][0])
            assert descriptor["restore_context"]["act_thread_id"] == "12345678901234560"
            assert descriptor["success"]["server_thread_key"] == 12345678901234560
            assert all(m.thread_id == "12345678901234560" for m in messages)

    def test_history_field_exception_maps_to_empty_list(self):
        """The live-observed behavior (2026-09, thread 12345678901234560, six
        name-guided attempts): without real e2ee raw_token enrollment the
        endpoint answers ONE deterministic field_exception (mid
        b9dead6ed672dfbc0d…) with an EMPTY row set — mapped to an empty
        history, never a crash. The contract query still executed."""
        service, session = make_service({
            "EBMessageRangeQueryForThreadsQuery": GraphQLProtocolError(
                "EBMessageRangeQueryForThreadsQuery: A server error "
                "field_exception occured. Check server logs for details.",
                raw=[{"message": "A server error field_exception occured.",
                      "severity": "ERROR",
                      "mids": ["b9dead6ed672dfbc0d6f5d621b7b22f0"],
                      "path": ["viewer", "encrypted_backup", "mailbox",
                               "messages_from_selected_threads"]}]),
        })
        assert service.history("12345678901234560") == []
        _, doc_id, variables = session.graphql.calls[0]
        assert doc_id == HISTORY_DOC_ID
        assert variables["restore_type"] == "RANGE_QUERY_RESTORE"

    def test_history_empty_thread_returns_empty_list(self):
        """The live empty response shape: the mailbox row set is a plain
        empty list, no errors."""
        canned = {"data": {"viewer": {"encrypted_backup": {
            "id": "1570327654890895",
            "mailbox": {"messages_from_selected_threads": []}}}}}
        service, _ = make_service({"EBMessageRangeQueryForThreadsQuery": canned})
        assert service.history("12345678901234560") == []

    def test_history_limit_via_query_arg_and_post_slice(self):
        """limit rides the query itself (query_num_messages) with a
        post-slice as belt and braces."""
        service, session = make_service(
            {"EBMessageRangeQueryForThreadsQuery": canned_history_response()})
        messages = service.history("12345678901234560", limit=2)
        assert [m.timestamp_ms for m in messages] == [
            1788113260000, 1788113250111]
        descriptor = json.loads(
            session.graphql.calls[0][2]["restore_payload_strings"][0])
        assert descriptor["success"]["query_num_messages"] == 2

    def test_history_fresh_device_id_per_call(self):
        service, session = make_service(
            {"EBMessageRangeQueryForThreadsQuery": canned_history_response()})
        service.history("12345678901234560")
        service.history("12345678901234560")
        first, second = (json.loads(c[2]["restore_payload_strings"][0])
                         for c in session.graphql.calls)
        d1 = first["success"]["device_context"]["device_id"]
        d2 = second["success"]["device_context"]["device_id"]
        assert d1 and d2 and d1 != d2

    def test_history_doc_id_present_in_harvested_registry(self):
        """The mandated history doc_id is loadable through the real
        harvested registry via the session."""
        session = StubSession()  # real assets/ registry
        assert session.registry.doc_id(
            "EBMessageRangeQueryForThreadsQuery") == HISTORY_DOC_ID

    @pytest.mark.live
    def test_history_live(self):
        """Live sanity: history() answers the probed EB range contract on the
        real thread (live-verified 2026-09: deterministic field_exception ->
        empty list for accounts without e2ee raw_token enrollment). Gated
        behind FBK_LIVE=1; never prints message bodies."""
        from session import Session
        service = MessengerService(Session(journal_name=None))
        messages = service.history("12345678901234560")
        assert isinstance(messages, list)
        assert all(isinstance(m, Message) for m in messages)


class TestSend:
    """Pins the send path: contract input shape, the V2 protocol-error
    fallback, and the E2EE NonSelfThreadError guard (docs/15 §P7)."""

    def test_send_substitutes_and_uses_contract_doc_id(self):
        service, session = make_service({
            "useCometAIHTSSendMessageMutation": canned_send_response(),
        })
        response = service.send("12345678901234560", "fbk protocol test message",
                               force=True)
        assert response["data"]["xfb_comet_ai_hts_send_message_mutation"]
        assert MessengerService.sent_ok(response) is True

        assert len(session.graphql.calls) == 1
        friendly, doc_id, variables = session.graphql.calls[0]
        assert friendly == "useCometAIHTSSendMessageMutation"
        assert doc_id == SEND_DOC_ID
        assert set(variables.keys()) == {"input"}
        sent = variables["input"]
        assert sent["message"] == "fbk protocol test message"
        assert sent["thread_id"] == "12345678901234560"
        assert set(sent.keys()) == set(DEFAULT_SEND_INPUT.keys())

    def test_send_fresh_message_id_per_call(self):
        service, session = make_service({
            "useCometAIHTSSendMessageMutation": canned_send_response(),
        })
        service.send("t_1", "one", force=True)
        service.send("t_1", "two", force=True)
        first, second = (c[2]["input"] for c in session.graphql.calls)
        assert first["message_id"] != second["message_id"]
        assert first["message_id"] and second["message_id"]

    def test_send_falls_back_to_v2_on_protocol_error(self):
        service, session = make_service({
            "useCometAIHTSSendMessageMutation":
                GraphQLProtocolError("1675012 noncoercible (canned)",
                                     code=1675012, raw=[{"code": 1675012}]),
            "useCometAIHTSSendMessageV2Mutation": {
                "data": {"xfb_conversational_support_send_message": [
                    {"id": "v2-1", "messages": [{"id": "m-2"}]}]}},
        })
        response = service.send("t_9", "fallback text", force=True)
        assert MessengerService.sent_ok(response) is True

        assert len(session.graphql.calls) == 2
        second_friendly, second_doc_id, second_variables = session.graphql.calls[1]
        assert second_friendly == "useCometAIHTSSendMessageV2Mutation"
        assert second_doc_id == SEND_V2_DOC_ID
        sent = second_variables["input"]
        assert sent["message"] == "fallback text"
        assert sent["thread_id"] == "t_9"
        assert set(sent.keys()) == set(DEFAULT_SEND_INPUT_V2.keys())

    def test_send_guard_blocks_non_self_thread(self):
        """docs/15 §P7: plaintext sends to P2P (E2EE) threads are refused."""
        service, _session = make_service({
            "useCometAIHTSSendMessageMutation": canned_send_response(),
        })
        with pytest.raises(MessengerService.NonSelfThreadError, match="E2EE"):
            service.send("12345678901234560", "should not go out")

    def test_send_guard_passes_self_thread_and_force(self):
        service, session = make_service({
            "useCometAIHTSSendMessageMutation": canned_send_response(),
        })
        # self thread (uid == stub user) needs no force
        service.send("12345678901234", "self note")
        # non-self thread is allowed with force=True
        service.send("12345678901234560", "experiment", force=True)
        assert len(session.graphql.calls) == 2

    def test_sent_ok_false_on_empty_response(self):
        assert MessengerService.sent_ok({}) is False
        assert MessengerService.sent_ok({"data": {}}) is False
        assert MessengerService.sent_ok(
            {"data": {"xfb_comet_ai_hts_send_message_mutation": []}}) is False


class TestRegistry:
    """Pins that the mandated messenger doc_ids resolve through the real
    harvested registry via the session."""

    def test_contract_doc_ids_present_in_harvested_registry(self):
        """The two mandated doc_ids (thread list + send) are loadable through
        the real harvested registry via the session."""
        session = StubSession()  # real assets/ registry
        assert session.registry.doc_id("MWCMBlendedThreadListQuery") == THREADLIST_DOC_ID
        assert session.registry.doc_id("useCometAIHTSSendMessageMutation") == SEND_DOC_ID
        assert session.registry.doc_id("useCometAIHTSSendMessageV2Mutation") == SEND_V2_DOC_ID
        assert session.registry.doc_id(
            "LSPlatformGraphQLLightspeedRequestQuery") == LIGHTSPEED_DOC_ID


class TestListen:
    """Live-gated MQTT/DGW listen sessions (FBK_LIVE=1 + cookies.txt):
    CONNACK/SUBACK/frame-pump structure, never message bodies."""

    @pytest.mark.live
    def test_listen_live_connects_and_collects(self):
        """Live MQTT session (docs/15 §P3-1): CONNACK + SUBACK + frame pump.
        Requires cookies.txt + network; gated behind FBK_LIVE=1."""
        from session import Session
        service = MessengerService(Session(journal_name=None))
        frames = service.listen(seconds=5.0)
        assert isinstance(frames, list)
        for frame in frames:
            assert frame["topic"] in ("/t_ms", "/t_rtc_multi")
            assert frame["kind"] == "PUBLISH"
            assert isinstance(frame["size"], int) and frame["size"] > 0
            assert isinstance(frame["summary"], dict)

    @pytest.mark.live
    def test_threads_live(self):
        """Live sanity: threads() on a real Session returns real threads.
        Gated behind FBK_LIVE=1; never prints message bodies."""
        from session import Session
        service = MessengerService(Session(journal_name=None))
        threads = service.threads(limit=5)
        assert isinstance(threads, list)
        assert all(isinstance(t, ThreadSummary) for t in threads)

    @pytest.mark.live
    def test_listen_dgw_live(self):
        """Live DGW lightspeed session (docs/15 §P3-7): handshake, subscribe,
        auto-acked typed responses. Gated behind FBK_LIVE=1."""
        from session import Session
        service = MessengerService(Session(journal_name=None))
        responses = service.listen_dgw(seconds=15.0)
        assert isinstance(responses, list)
        for response in responses:
            assert response["payload_type"] in (
                "client_subscribe", "state_sync", "state_sync_delta",
                "state_sync_result")
            assert isinstance(response["payload"], str)
            assert "{" in response["payload"]


# --------------------------------------------------------------------------
# DGW lightspeed request semantics (docs/15 §P3-7) — replayed against the
# REAL full-byte capture in assets/messenger_ws_full.json.
# --------------------------------------------------------------------------

#: Field-name catalog decoded from the capture — subscribe_threads must
#: reproduce these EXACT key sets (nothing invented).
CATALOG_ENVELOPE_KEYS = {"app_id", "payload", "request_id", "type"}
CATALOG_TASK_KEYS = {"failure_count", "label", "payload", "queue_name", "task_id"}
CATALOG_TASKS_KEYS = {"epoch_id", "tasks", "version_id"}


def _lightspeed_sock(ws_capture) -> dict:
    return next(s for s in ws_capture
                if s["url"].split("?")[0].endswith("/lightspeed"))


def _lightspeed_opener(ws_capture) -> bytes:
    """The real 876B sent WS message opening the lightspeed socket:
    a 0x0F control {} frame CONCATENATED with the first 0x0D data frame."""
    sock = _lightspeed_sock(ws_capture)
    return base64.b64decode(next(f["b64"] for f in sock["frames"]
                                 if f["dir"] == "sent" and f["n"] == 876))


def _lightspeed_recv_data(ws_capture, request_id: int) -> bytes:
    """A real server data frame echoing the given request_id."""
    for frame in _lightspeed_sock(ws_capture)["frames"]:
        if frame["dir"] != "recv":
            continue
        raw = base64.b64decode(frame["b64"])
        if raw[0] == DGW_OP_DATA and f'"request_id":{request_id},'.encode() in raw:
            return raw
    raise AssertionError(f"no recv data frame with request_id {request_id}")


class _FakeWS:
    """Records sends; stands in for the websockets sync client."""

    def __init__(self):
        self.sent: list[bytes] = []

    def send(self, data: bytes) -> None:
        self.sent.append(data)


class TestDGWSplitFrame:
    """Pins split_ws_frame against the REAL 876B concatenated capture and
    the bare 3-byte ACK3 shapes."""

    def test_splits_real_concatenated_capture(self, ws_capture):
        """The REAL 876B opener: control {} + client_subscribe data packet."""
        raw = _lightspeed_opener(ws_capture)
        assert len(raw) == 876
        frames = split_ws_frame(raw)
        assert len(frames) >= 2
        assert frames[0].op == DGW_OP_CONTROL
        assert frames[0].json() == {}
        assert frames[-1].op == DGW_OP_DATA
        assert frames[-1].kind == "DATA"

    def test_ack3_is_three_bare_bytes(self):
        frames = split_ws_frame(bytes.fromhex("0e1200"))
        assert len(frames) == 1
        assert frames[0].op == DGW_OP_ACK3
        assert frames[0].seq == 0x12

    def test_concatenation_with_ack3(self):
        """A data frame followed by the real 3-byte ACK3 from the capture
        (the recv stream interleaves both shapes in one WS message)."""
        data = DGWFrame(op=DGW_OP_DATA, seq=7, flags=0, payload=b"x")
        frames = split_ws_frame(data.encode() + bytes.fromhex("0e0f00"))
        assert [f.op for f in frames] == [DGW_OP_DATA, DGW_OP_ACK3]
        assert frames[1].seq == 0x0F


class TestDecodeRequestPayload:
    """Pins request typing off the wire: catalog envelope/task field
    names and the request_id correlation rule."""

    def test_real_request_frame_types_as_client_subscribe(self, ws_capture):
        frames = split_ws_frame(_lightspeed_opener(ws_capture))
        request = decode_request_payload(frames[-1])
        assert isinstance(request, DGWRequest)
        assert request.payload_type == "client_subscribe"
        assert request.request_id == 4
        # inner payload decoded from the envelope's JSON string
        assert set(request.payload.keys()) == CATALOG_TASKS_KEYS
        assert request.payload["version_id"] == "28803601955930286"
        tasks = request.payload["tasks"]
        assert all(set(t.keys()) == CATALOG_TASK_KEYS for t in tasks)
        assert tasks[0]["queue_name"] == "trq"
        assert json.loads(tasks[0]["payload"])["sync_group"] == 1

    def test_real_response_frame_types_as_state_sync_result(self, ws_capture):
        """The server reply echoing request_id 4 — the correlation rule."""
        raw = _lightspeed_recv_data(ws_capture, request_id=4)
        request = decode_request_payload(DGWFrame.decode(raw))
        assert request.payload_type == "state_sync_result"
        assert request.request_id == 4
        assert "step" in request.payload

    def test_rejects_non_data_frames(self):
        with pytest.raises(DGWRequestError):
            decode_request_payload(DGWFrame.control(0, {}))

    def test_rejects_unknown_request_type(self):
        body = b'{"app_id":"x","request_id":1,"type":99,"payload":"{}"}'
        frame = DGWFrame(op=DGW_OP_DATA, seq=1, flags=0,
                         payload=b"\x00\x80" + body)
        with pytest.raises(DGWRequestError):
            decode_request_payload(frame)


class TestSubscribeThreads:
    """Pins subscribe_threads: catalog field names verbatim, fresh
    snowflake epoch ids per call, and auto-incrementing request ids."""

    def _client(self) -> LightspeedClient:
        client = LightspeedClient({"c_user": "12345678901234", "xs": "test-xs"})
        client._connected = True
        client._ws = _FakeWS()
        return client

    def _sent_request(self, client) -> DGWRequest:
        (wire,) = client._ws.sent
        frames = split_ws_frame(wire)
        assert [f.op for f in frames] == [DGW_OP_CONTROL, DGW_OP_DATA]
        return decode_request_payload(frames[-1])

    def test_builds_client_subscribe_with_catalog_field_names(self):
        client = self._client()
        device_id = str(uuid.uuid4())
        request = client.subscribe_threads(
            device_id, [DGW_DEFAULT_THREAD_ID], request_id=1)
        assert isinstance(request, DGWRequest)
        assert request.payload_type == "client_subscribe"
        assert request.request_id == 1
        assert client.device_id == device_id

        sent = self._sent_request(client)
        assert sent.request_id == 1
        assert sent.payload_type == "client_subscribe"
        # the catalog envelope + task shapes, checked on the decoded wire
        (wire,) = client._ws.sent
        data_frame = next(f for f in split_ws_frame(wire)
                          if f.op == DGW_OP_DATA)
        envelope = json.loads(data_frame.payload[data_frame.payload.find(b"{"):])
        assert set(envelope.keys()) == CATALOG_ENVELOPE_KEYS
        assert envelope["app_id"] == "2220391788200892"
        assert set(sent.payload.keys()) == CATALOG_TASKS_KEYS
        tasks = sent.payload["tasks"]
        assert all(set(t.keys()) == CATALOG_TASK_KEYS for t in tasks)
        # thread-list task (queue "trq", sync_group 1) + per-thread task
        assert tasks[0]["queue_name"] == "trq"
        thread_payloads = [json.loads(t["payload"]) for t in tasks[1:]]
        assert thread_payloads == [
            {"thread_key": 12345678901234560, "sync_group": 95}]

    def test_fresh_epoch_id_per_call_and_snowflake_shape(self):
        client = self._client()
        first = client.subscribe_threads("d", ["12345678901234560"])
        second = client.subscribe_threads("d", ["12345678901234560"])
        assert first.payload["epoch_id"] != second.payload["epoch_id"]
        # the captured epoch ids are unix_ms << 22 snowflakes — ours too
        for request in (first, second):
            unix_ms = request.payload["epoch_id"] >> 22
            assert abs(unix_ms / 1000 - time.time()) < 60

    def test_auto_request_ids_increment_from_one(self):
        client = self._client()
        assert client.request("state_sync", {"database": 2}).request_id == 1
        assert client.request("state_sync", {"database": 7}).request_id == 2

    def test_unknown_payload_type_rejected(self):
        client = self._client()
        with pytest.raises(DGWRequestError):
            client.request("not_in_catalog", {})
