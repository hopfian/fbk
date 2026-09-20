"""DGW (data-gateway) WebSocket transport — modern Messenger (docs/15 §P2-4, §P3-6).

The bidirectional GraphQL-over-WS family at
wss://gateway.facebook.com/ws/<channel> (rpsignaling, realtime,
lightspeed, streamcontroller). This package bundles the channel-agnostic
socket client (``client``), the frame codec (``frames``), and the
lightspeed request/response dialect with its pure decode helpers
(``requests``); re-exported here so callers depend on the package, not
the submodules.

USER-DOC ANCHOR: cli/docs/08-architecture.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from .client import DGWClient  # noqa: F401
from .frames import DGWFrame  # noqa: F401
from .requests import (  # noqa: F401
    DGWRequest,
    DGWRequestError,
    LightspeedClient,
    decode_request_payload,
    split_ws_frame,
)
