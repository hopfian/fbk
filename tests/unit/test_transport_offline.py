"""Offline transport tests: canonical header sets (docs/09 §1-2), cookie
header ordering, and FBTransport journaling/governor wiring with the
HTTP session stubbed out. No network: impersonate is pinned at
construction (skipping resolve_impersonate's probe) and the curl_cffi
session object is replaced by a recording fake.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fakes import StubTransportResponse

import constants as C
from governor import GovernorBlockedError
from journal.recorder import JSONLJournal
from transport.cookies import cookies_header
from transport.headers import graphql_headers, page_headers
from transport.profile import ClientProfile
from transport.session import FBTransport, FingerprintRejectedError


class TestHeaderSets:
    """Pins the canonical page/GraphQL header sets and cookie-header
    serialization (docs/09 §1-2)."""

    def test_page_headers_pin_the_profile_locale(self):
        profile = ClientProfile(accept_language="bn-BD,bn;q=0.9")
        assert page_headers(profile) == {"accept-language": "bn-BD,bn;q=0.9"}

    def test_graphql_headers_shape_with_lsd(self):
        profile = ClientProfile()
        headers = graphql_headers(profile, "CometTestQuery", "TESTLSD")
        assert headers == {
            "content-type": "application/x-www-form-urlencoded",
            "origin": C.DOMAINS["www"],
            "referer": C.DOMAINS["www"] + "/",
            "accept-language": profile.accept_language,
            "x-fb-friendly-name": "CometTestQuery",
            "x-fb-lsd": "TESTLSD",
        }

    def test_graphql_headers_omit_lsd_when_absent(self):
        headers = graphql_headers(ClientProfile(), "CometTestQuery", None)
        assert "x-fb-lsd" not in headers
        assert headers["x-fb-friendly-name"] == "CometTestQuery"

    def test_cookies_header_is_sorted(self):
        assert cookies_header({"b": "2", "a": "1", "c": "3"}) == "a=1; b=2; c=3"

    def test_cookies_header_empty_jar(self):
        assert cookies_header({}) == ""


class RecordingGovernor:
    def __init__(self):
        self.calls: list[tuple] = []

    def before_request(self, *, is_mutation: bool = False, quiet: bool = False):
        self.calls.append(("before", is_mutation))

    def observe_soft_block(self):
        self.calls.append(("soft_block",))


class BlockingGovernor:
    def before_request(self, *, is_mutation: bool = False, quiet: bool = False):
        raise GovernorBlockedError("cooldown active for 900s")

    def observe_soft_block(self):
        raise AssertionError("a blocked request never sees a response")


class FakeHTTPSession:
    """Replaces FBTransport._session: records calls, replays one response."""

    def __init__(self, response: Any):
        self.response = response
        self.calls: list[tuple] = []

    def get(self, url: str, **kw):
        self.calls.append(("GET", url, kw))
        return self.response

    def post(self, url: str, **kw):
        self.calls.append(("POST", url, kw))
        return self.response


def make_transport(tmp_path: Path, response: Any,
                   governor: Any = None) -> FBTransport:
    transport = FBTransport(
        {"c_user": "1", "xs": "SECRET-XS", "datr": "SECRET-DATR"},
        profile=ClientProfile(),
        journal=JSONLJournal(tmp_path / "j.jsonl"),
        timeout=5, impersonate="chrome136", governor=governor)
    transport._session = FakeHTTPSession(response)
    return transport


class TestFBTransportWiring:
    """Pins FBTransport's governor gating and journal wiring with the
    curl session stubbed out: mutation flag propagation, pre-HTTP
    blocking, soft-block observation, and the journal entry shape."""

    def test_incoherent_profile_rejected_at_construction(self, tmp_path):
        with pytest.raises(FingerprintRejectedError):
            FBTransport({"c_user": "1"}, profile=ClientProfile(
                sec_ch_ua_platform='"Android"'))

    def test_next_q_increments(self, tmp_path):
        transport = FBTransport({"c_user": "1"}, impersonate="chrome136")
        assert transport.next_q() == "1"
        assert transport.next_q() == "2"

    def test_post_graphql_sends_canonical_headers(self, tmp_path):
        resp = StubTransportResponse(text="ok", url="https://www.facebook.com/api/graphql/")
        transport = make_transport(tmp_path, resp)
        transport.post_graphql("https://www.facebook.com/api/graphql/",
                              data={"doc_id": "1"}, friendly_name="Q",
                              lsd="TESTLSD")
        method, url, kw = transport._session.calls[0]
        assert (method, url) == ("POST", "https://www.facebook.com/api/graphql/")
        assert kw["headers"]["content-type"] == "application/x-www-form-urlencoded"
        assert kw["headers"]["x-fb-friendly-name"] == "Q"
        assert kw["headers"]["x-fb-lsd"] == "TESTLSD"
        assert kw["data"] == {"doc_id": "1"}

    def test_post_graphql_without_lsd_omits_header(self, tmp_path):
        resp = StubTransportResponse(text="ok")
        transport = make_transport(tmp_path, resp)
        transport.post_graphql("https://x", data={}, friendly_name="Q", lsd=None)
        assert "x-fb-lsd" not in transport._session.calls[0][2]["headers"]

    def test_governor_receives_the_mutation_flag(self, tmp_path):
        resp = StubTransportResponse(text="ok")
        gov = RecordingGovernor()
        transport = make_transport(tmp_path, resp, governor=gov)
        transport.post_graphql("https://x", data={}, friendly_name="XMutation",
                               lsd=None, is_mutation=True)
        transport.post_graphql("https://x", data={}, friendly_name="ReadQuery",
                               lsd=None, is_mutation=False)
        assert gov.calls == [("before", True), ("before", False)]

    def test_blocked_governor_stops_the_request_before_http(self, tmp_path):
        resp = StubTransportResponse(text="ok")
        transport = make_transport(tmp_path, resp, governor=BlockingGovernor())
        with pytest.raises(GovernorBlockedError):
            transport.post_graphql("https://x", data={}, friendly_name="Q", lsd=None)
        assert transport._session.calls == []

    def test_empty_200_flips_governor_soft_block_via_post(self, tmp_path):
        resp = StubTransportResponse(status_code=200, text="")
        gov = RecordingGovernor()
        transport = make_transport(tmp_path, resp, governor=gov)
        transport.post_graphql("https://x", data={}, friendly_name="Q", lsd=None)
        assert ("soft_block",) in gov.calls

    def test_non_empty_200_does_not_flag_the_governor(self, tmp_path):
        resp = StubTransportResponse(status_code=200, text='{"data":{}}')
        gov = RecordingGovernor()
        transport = make_transport(tmp_path, resp, governor=gov)
        transport.post_graphql("https://x", data={}, friendly_name="Q", lsd=None)
        assert gov.calls == [("before", False)]

    def test_journal_records_url_status_and_length(self, tmp_path):
        resp = StubTransportResponse(text="hello world", url="https://www.facebook.com/")
        transport = make_transport(tmp_path, resp)
        transport.get("https://www.facebook.com/")
        entries = JSONLJournal(tmp_path / "j.jsonl").read_all()
        assert len(entries) == 1
        entry = entries[0]
        assert entry["method"] == "GET"
        assert entry["url"] == "https://www.facebook.com/"
        assert entry["status"] == 200
        assert entry["content_length"] == 11

    def test_journal_never_sees_cookie_values(self, tmp_path):
        resp = StubTransportResponse(text="ok")
        transport = make_transport(tmp_path, resp)
        transport.get("https://www.facebook.com/")
        disk = (tmp_path / "j.jsonl").read_text(encoding="utf-8")
        assert "SECRET-XS" not in disk
        assert "SECRET-DATR" not in disk
        assert json.loads(disk.splitlines()[0])["method"] == "GET"
