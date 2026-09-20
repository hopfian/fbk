"""Transport raw seam — the EXACT concurrent-agent contract (wave 2).

FBTransport gains two escape hatches for non-GraphQL surfaces (upload,
video upload) that speak their own HTTP dialects:

  * raw_post(url, *, data: bytes | dict, headers: Mapping[str, str],
             surface: str = "raw")
  * raw_get(url, *, headers: Mapping[str, str], surface: str = "raw")

Pinned semantics (tests/unit — offline, deterministic, the curl session
stubbed exactly like test_transport_offline.py):

  1. rides the curl_cffi session with the CALLER's headers verbatim
     (no page_headers() default is ever injected);
  2. NOT governor-gated: before_request() is never called;
  3. DOES feed the soft-block observer (empty-200 / 403 / 429 ->
     governor.observe_soft_block());
  4. journals {"method", "url", "status", "content_length",
     "ctx": {"surface": <surface>}} when a journal is attached.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fakes import StubTransportResponse

from journal.recorder import JSONLJournal
from transport.profile import ClientProfile
from transport.session import FBTransport


class UngatedGovernor:
    """A governor whose gate EXPLODES: proves the raw seam never calls it."""

    def before_request(self, *, is_mutation: bool = False, quiet: bool = False):
        raise AssertionError("raw_post/raw_get must not be governor-gated")

    def __init__(self):
        self.soft_blocks = 0

    def observe_soft_block(self):
        self.soft_blocks += 1


class FakeHTTPSession:
    """Replaces FBTransport._session: records calls, replays one response."""

    def __init__(self, response):
        self.response = response
        self.calls: list[tuple] = []

    def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        return self.response

    def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        return self.response

    def close(self):
        pass


def make_transport(response=None, *, journal=None, governor=None) -> FBTransport:
    transport = FBTransport(
        {"c_user": "1", "xs": "SECRET-XS"},
        profile=ClientProfile(),
        journal=journal, timeout=5, impersonate="chrome136", governor=governor)
    transport._session = FakeHTTPSession(response)
    return transport


RAW_URL = "https://upload.facebook.com/api/upload/"
CALLER_HEADERS = {"content-type": "application/octet-stream",
                  "x-custom-header": "yes"}


# ------------------------------------------------------------- call semantics
class TestRawGet:
    """Pins raw_get: caller headers verbatim (no page_headers default),
    no governor gate, and operation without governor or journal."""

    def test_passes_caller_headers_verbatim_no_default_injected(self):
        transport = make_transport(StubTransportResponse(text="ok"))
        transport.raw_get(RAW_URL, headers=CALLER_HEADERS)
        method, url, kw = transport._session.calls[0]
        assert (method, url) == ("GET", RAW_URL)
        # the caller's mapping rides as-is — page_headers() default absent
        assert kw["headers"] == CALLER_HEADERS
        assert "accept-language" not in kw["headers"]

    def test_is_not_governor_gated(self):
        gov = UngatedGovernor()
        transport = make_transport(StubTransportResponse(text="ok"), governor=gov)
        transport.raw_get(RAW_URL, headers=CALLER_HEADERS)  # must not raise
        assert gov.soft_blocks == 0

    def test_works_without_governor_or_journal(self):
        transport = make_transport(StubTransportResponse(text="body"))
        resp = transport.raw_get(RAW_URL, headers={})
        assert resp.text == "body"


class TestRawPost:
    """Pins raw_post: dict and bytes payloads verbatim, caller headers,
    no governor gate."""

    def test_dict_data_is_sent(self):
        transport = make_transport(StubTransportResponse(text="ok"))
        transport.raw_post(RAW_URL, data={"field": "value", "n": "2"},
                          headers=CALLER_HEADERS)
        method, url, kw = transport._session.calls[0]
        assert (method, url) == ("POST", RAW_URL)
        assert kw["data"] == {"field": "value", "n": "2"}
        assert kw["headers"] == CALLER_HEADERS

    def test_bytes_data_is_sent_verbatim(self):
        transport = make_transport(StubTransportResponse(text="ok"))
        payload = b"\x00\x01binary\xff\xff"
        transport.raw_post(RAW_URL, data=payload, headers=CALLER_HEADERS)
        method, _url, kw = transport._session.calls[0]
        assert method == "POST"
        assert kw["data"] == payload
        assert kw["headers"] == CALLER_HEADERS

    def test_is_not_governor_gated(self):
        gov = UngatedGovernor()
        transport = make_transport(StubTransportResponse(text="ok"), governor=gov)
        transport.raw_post(RAW_URL, data=b"x", headers={})  # must not raise
        assert gov.soft_blocks == 0


# ------------------------------------------------- soft-block observation wiring
class TestRawSoftBlockObservation:
    """Pins the soft-block observer wiring: empty-200/403/429 flag the
    governor; a populated 200 and a 500 do not."""

    def _raw(self, transport, *, post):
        if post:
            transport.raw_post(RAW_URL, data=b"x", headers=CALLER_HEADERS)
        else:
            transport.raw_get(RAW_URL, headers=CALLER_HEADERS)

    @pytest.mark.parametrize("status", [200, 403, 429])
    def test_empty_200_and_403_429_flag_the_governor(self, status):
        gov = UngatedGovernor()
        resp = StubTransportResponse(status_code=status, text="")
        transport = make_transport(resp, governor=gov)
        self._raw(transport, post=False)
        self._raw(transport, post=True)
        assert gov.soft_blocks == 2  # both the GET and the POST observed it

    def test_populated_200_does_not_flag_the_governor(self):
        gov = UngatedGovernor()
        resp = StubTransportResponse(status_code=200, text='{"data":{}}')
        transport = make_transport(resp, governor=gov)
        self._raw(transport, post=False)
        assert gov.soft_blocks == 0

    def test_500_is_not_a_soft_block_signal(self):
        gov = UngatedGovernor()
        transport = make_transport(
            StubTransportResponse(status_code=500, text="err"), governor=gov)
        self._raw(transport, post=False)
        assert gov.soft_blocks == 0


# -------------------------------------------------------------- journal shape
class TestRawJournalShape:
    """Pins the raw journal entry contract shape (ctx.surface) and the
    no-secrets-in-journal guarantee."""

    def _journal(self, tmp_path):
        return JSONLJournal(tmp_path / "raw.jsonl")

    def test_raw_get_journals_the_contract_shape(self, tmp_path):
        journal = self._journal(tmp_path)
        transport = make_transport(
            StubTransportResponse(text="hello!", url=RAW_URL), journal=journal)
        transport.raw_get(RAW_URL, headers=CALLER_HEADERS)
        entry = journal.read_all()[0]
        assert set(entry) == {"ts", "method", "url", "status",
                              "content_length", "ctx"}
        assert entry["method"] == "GET"
        assert entry["url"] == RAW_URL
        assert entry["status"] == 200
        assert entry["content_length"] == 6
        assert entry["ctx"] == {"surface": "raw"}  # the default surface

    def test_raw_post_journals_with_the_caller_surface(self, tmp_path):
        journal = self._journal(tmp_path)
        transport = make_transport(
            StubTransportResponse(text="done", url=RAW_URL), journal=journal)
        transport.raw_post(RAW_URL, data=b"payload", headers=CALLER_HEADERS,
                           surface="upload")
        entry = journal.read_all()[0]
        assert entry["method"] == "POST"
        assert entry["status"] == 200
        assert entry["content_length"] == 4
        assert entry["ctx"] == {"surface": "upload"}

    def test_raw_get_custom_surface_reaches_the_journal(self, tmp_path):
        journal = self._journal(tmp_path)
        transport = make_transport(
            StubTransportResponse(text="x", url=RAW_URL), journal=journal)
        transport.raw_get(RAW_URL, headers=CALLER_HEADERS, surface="video_upload")
        assert journal.read_all()[0]["ctx"] == {"surface": "video_upload"}

    def test_raw_secrets_never_reach_the_journal(self, tmp_path):
        journal = self._journal(tmp_path)
        transport = make_transport(
            StubTransportResponse(text="ok", url=RAW_URL), journal=journal)
        transport.raw_post(RAW_URL, data={"xs": "SECRET-XS-IN-BODY"},
                           headers=CALLER_HEADERS)
        disk = (tmp_path / "raw.jsonl").read_text(encoding="utf-8")
        assert "SECRET-XS-IN-BODY" not in disk
