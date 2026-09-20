"""MQTT-over-WebSocket transport (docs/06, docs/15 §P2-4, §P3-1).

The MQIsdp dialect (protocol name "MQIsdp", level 3 — NOT standard
MQTT v3.1.1) over wss://edge-chat.facebook.com/chat. This package bundles
the session client (``client``), the CONNECT identity builders
(``connect``), the frame codec (``frames``), and the Thrift-compact
reader (``thrift``) for /t_ms PUBLISH bodies; re-exported here so
callers depend on the package, not the submodules.

USER-DOC ANCHOR: cli/docs/08-architecture.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from .client import MQTTClient  # noqa: F401
from .connect import build_fb_connect, build_identity_json  # noqa: F401
from .frames import (  # noqa: F401
    MQTTDecodeError,
    decode_connack,
    decode_suback,
    encode_pingreq,
    encode_publish,
    encode_subscribe,
    parse_packet,
    remaining_length,
)
from .thrift import CompactReader  # noqa: F401
