"""Realtime transports: MQTT-over-WS + DGW WebSocket (docs/06, docs/15 §P2-4).

ARCHITECTURE:
    Two coexisting push channels, both routed through the fingerprint-
    coherent WebSocket factory ``realtime.ws.coherent_ws_connect`` (the
    Phase-10 transport unification, docs/16 §2): the legacy MQIsdp message
    bus (``MQTTClient``, wss://edge-chat.facebook.com/chat) and the modern
    DGW data-gateway family (``DGWClient``/``LightspeedClient``,
    wss://gateway.facebook.com/ws/<channel>). This package re-exports the
    full public surface — clients, frame codecs, and the Thrift-compact
    reader — so command layers never import from the protocol submodules
    directly (docs/12 §2 layering rule).

Public API:
  * MQTTClient: Messenger MQTT bus client (MQIsdp handshake)
  * DGWClient: DGW gateway client (lightspeed/realtime/etc.)
  * LightspeedClient: the lightspeed request dialect
  * Frame codecs: DGWFrame, MQTT packet encode/decode, Thrift reader
  * coherent_ws_connect: fingerprint-coherent WebSocket factory

USER-DOC ANCHOR: cli/docs/08-architecture.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from .dgw.client import DGWClient, DGWError
from .dgw.frames import DGWFrame
from .dgw.requests import (
    LightspeedClient,
    decode_request_payload,
    split_ws_frame,
)
from .mqtt.client import MQTTClient, MQTTClientError
from .mqtt.connect import build_fb_connect, build_identity_json
from .mqtt.frames import (
    decode_connack,
    decode_suback,
    encode_publish,
    encode_subscribe,
    remaining_length,
)
from .mqtt.thrift import CompactReader, ThriftError
from .ws import coherent_ws_connect

__all__ = [
    "CompactReader",
    "DGWClient",
    "DGWError",
    "DGWFrame",
    "LightspeedClient",
    "MQTTClient",
    "MQTTClientError",
    "ThriftError",
    "build_fb_connect",
    "build_identity_json",
    "coherent_ws_connect",
    "decode_connack",
    "decode_request_payload",
    "decode_suback",
    "encode_publish",
    "encode_subscribe",
    "remaining_length",
    "split_ws_frame",
]
