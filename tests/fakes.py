"""Stubs for offline service tests.

StubSession quacks like fbk.session.Session for every surface service:
  * .graphql        -> StubGraphQLClient with canned per-friendly responses
  * .registry       -> real DocIdRegistry loaded from assets (or inline pairs)
  * .bootstrap()    -> a synthetic logged-in Bootstrap
  * .cookies / .user_id()
"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from auth.bootstrap import Bootstrap
from auth.state import LoginState
from graphql.registry import DocIdRegistry
from transport.cookies import load_netscape

PROJECT_ROOT = Path(__file__).resolve().parents[1]  # cli/
ASSETS = PROJECT_ROOT / "data"  # cli/data/ (the self-contained data directory)


class StubGraphQLClient:
    """Canned GraphQLClient: responses keyed by friendly_name.

    A canned value may be:
      * a dict        — returned as the merged payload
      * an Exception  — raised (typed errors flow through service code)
      * a list        — treated as streamed docs and deep-merged
    """

    def __init__(self, responses: Mapping[str, Any], *, strict: bool = True):
        self.responses = dict(responses)
        self.strict = strict
        self.calls: list[tuple[str, str, dict]] = []

    def call(self, friendly_name: str, doc_id: str, variables: Mapping[str, Any],
             **_kw: Any) -> dict[str, Any]:
        self.calls.append((friendly_name, doc_id, dict(variables)))
        if friendly_name not in self.responses:
            if self.strict:
                raise AssertionError(
                    f"StubGraphQLClient: unexpected call to {friendly_name!r}")
            return {}
        canned = self.responses[friendly_name]
        if isinstance(canned, Exception):
            raise canned
        if isinstance(canned, list):
            from graphql.parsing import merge_docs
            return merge_docs(canned)
        return dict(canned)

    def call_by_name(self, friendly_name: str, variables: Mapping[str, Any],
                     **kw: Any) -> dict[str, Any]:
        return self.call(friendly_name, "<registry>", variables, **kw)

    # the real client exposes these; services may check them
    @property
    def bootstrap(self) -> Bootstrap:  # pragma: no cover - service-compatible shim
        raise AttributeError("use StubSession.bootstrap()")


class StubSession:
    """Session look-alike for offline service tests.

    Stubs the SERVICE plane, one level above StubTransport: surface
    services see ``.graphql``, ``.registry``, ``.bootstrap()``,
    ``.user_id()`` and ``.cookies`` and never notice the difference.

    Args:
        responses: Canned payloads keyed by friendly_name, handed to the
            StubGraphQLClient as-is (dicts, streamed lists, Exceptions).
        registry_pairs: When given, replaces the default real-asset
            registry with a hermetic DocIdRegistry.from_pairs() one —
            use this to pin doc_ids the asset registry may lack.
        cookies_path: When given, loads a real Netscape jar instead of
            the canned c_user/xs/datr test triple.
    """

    def __init__(self, responses: Mapping[str, Any] | None = None,
                 registry_pairs: dict[str, str] | None = None,
                 *, cookies_path: Path | None = None):
        self.graphql = StubGraphQLClient(responses or {})
        if registry_pairs is not None:
            self.registry = DocIdRegistry.from_pairs(registry_pairs)
        else:
            from config import Config
            try:
                self.registry = DocIdRegistry.from_assets(Config.discover().assets_dir)
            except Exception:
                self.registry = DocIdRegistry.from_pairs({})
        self.cookies = (load_netscape(cookies_path)
                        if cookies_path else {"c_user": "12345678901234",
                                               "xs": "test-xs", "datr": "test-datr"})
        self._bootstrap = Bootstrap(
            state=LoginState.LOGGED_IN,
            fb_dtsg="NAfTEST:1:1789723298",
            lsd="TESTLSD00000000000",
            user_id="12345678901234",
            user_name="Test User",
            revision="1047868043",
            preloads=[],
        )

    def bootstrap(self, *, force: bool = False) -> Bootstrap:
        return self._bootstrap

    def user_id(self) -> str:
        return "12345678901234"


class StubTransportResponse:
    """Minimal response object for transport-level client tests.

    Args:
        status_code: HTTP status the stub reports (soft-block routing).
        text: Body text; ``.content`` is its UTF-8 encoding and ``.json()``
            parses it (so GraphQLClient error envelopes work verbatim).
        url: Final URL the stub claims (redirect assertions).
        headers: Response headers — notably ``location`` for 301/302
            routing tests.
    """

    def __init__(self, *, status_code: int = 200, text: str = "", url: str = "",
                 headers: Mapping[str, str] | None = None):
        self.status_code = status_code
        self.text = text
        self.url = url
        self.headers = dict(headers or {})
        self.content = text.encode()

    def json(self) -> Any:
        import json
        return json.loads(self.text)


class StubTransport:
    """Queued-response transport for GraphQLClient unit tests.

    Stubs the TRANSPORT plane (where StubSession stubs the service
    plane): GraphQLClient drives ``post_graphql()`` directly, each call
    popping one queued StubTransportResponse. When a response is an
    Exception it is raised at pop time (simulating transport failure).
    ``posts`` records every (friendly_name, data, lsd) so tests can
    assert the exact wire body the client assembled.
    """

    def __init__(self, queue: list[Any]):
        self.queue = list(queue)
        self.cookies = {"c_user": "1", "xs": "x", "datr": "d"}
        self._q = 0
        self.posts: list[dict] = []

    def next_q(self) -> str:
        """The per-transport query ordinal, sent as the ``q`` form field."""
        self._q += 1
        return str(self._q)

    def post_graphql(self, url: str, *, data, friendly_name: str, lsd, **kw):
        self.posts.append({"friendly_name": friendly_name, "data": dict(data),
                           "lsd": lsd})
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item
