"""The fb CONNECT identity blob — live-decoded structure (docs/15 §P3-1).

The MQTT username of the CONNECT is plain-UTF-8 JSON:

    {"a": <user-agent>, "asi": null, "aid": <app id>, "aids": null,
     "chat_on": false, "cp": 3, "ct": "websocket", "d": <device uuid>,
     "dc": "", "ecp": 10, "fg": true, "gas": null, "mqtt_sid": "",
     "no_auto_fg": true, "p": null, "pack": [], "php_override": "",
     "pm": [], "s": <session int>, "st": [], "u": <c_user>}

Both a verbatim-replay helper (captured bytes) and a fresh-identity builder
are provided; the broker accepts both (P3-1 proof).

CALIBRATION NOTES (docs/15 §P3-1):
  The captured 452B CONNECT carries this blob verbatim after the
  ``mqttwsclient`` client id (``01 a5`` = field marker + length prefix).
  Field semantics beyond the obvious (``a`` UA, ``aid`` app id, ``d``
  device uuid, ``s`` session int, ``u`` uid) are replayed verbatim —
  ``cp``/``ecp`` and the null-valued keys have no attributed server-side
  meaning in the ground truth, so a rebuilt identity keeps them
  byte-identical to the capture. The UA is sanitized before it ships
  (docs/15 §P9-2: the capture rode ``HeadlessChrome/152`` because the
  capture browser ran headless).

USER-DOC ANCHOR: cli/docs/08-architecture.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import json
import random
import uuid
from typing import Any

import constants as C
from transport.profile import sanitize_user_agent

from .frames import build_connect


def build_identity_json(
    user_id: str,
    *,
    user_agent: str | None = None,
    device_id: str | None = None,
    session_id: int | None = None,
    app_id: str = C.MESSENGER_WEB_APP_ID,
    foreground: bool = True,
) -> dict[str, Any]:
    """A fresh identity blob, structurally identical to the live-captured one.

    Every key of the P3-1 capture is reproduced; unknown-semantics fields
    (``cp``, ``ecp``, the null-valued keys) keep their captured values
    verbatim so a rebuilt CONNECT differs from the production one only in
    the identity material (UA, device uuid, session int, uid).

    Args:
        user_id: The operator's numeric c_user id (the blob's ``u``).
        user_agent: Optional UA override, sanitized before use; defaults
            to the chrome136 desktop shape.
        device_id: Optional device uuid (the blob's ``d``); a fresh uuid4
            is generated when None.
        session_id: Optional session int (the blob's ``s``); a fresh
            random int in the captured magnitude class is generated when
            None.
        app_id: The Messenger web app id (``aid``); defaults to the
            captured ``constants.MESSENGER_WEB_APP_ID``.
        foreground: The ``fg`` foreground flag; True as captured.

    Returns:
        The identity dict, ready for JSON encoding into the CONNECT
        username field.
    """
    return {
        "a": sanitize_user_agent(user_agent or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36")),
        "asi": None,
        "aid": int(app_id),
        "aids": None,
        "chat_on": False,
        "cp": 3,
        "ct": "websocket",
        "d": device_id or str(uuid.uuid4()),
        "dc": "",
        "ecp": 10,
        "fg": foreground,
        "gas": None,
        "mqtt_sid": "",
        "no_auto_fg": True,
        "p": None,
        "pack": [],
        "php_override": "",
        "pm": [],
        # Fresh session int (the blob's "s") — random in [2**50, 2**53):
        # the same magnitude class as the captured session ints, and
        # safely below the 2**53 boundary. Exact captured value range is
        # not recorded in docs/15 §P3-1 (ground-truth gap).
        "s": session_id if session_id is not None else random.randrange(2 ** 50, 2 ** 53),
        "st": [],
        "u": user_id,
    }


def build_fb_connect(
    user_id: str,
    *,
    user_agent: str | None = None,
    device_id: str | None = None,
    session_id: int | None = None,
    client_id: bytes = C.MQTT_CLIENT_ID_PREFIX.encode(),
    keepalive: int = C.MQTT_KEEPALIVE_S,
) -> bytes:
    """A complete, forgeable MQIsdp CONNECT with a fresh identity.

    The P3-1 rebuilt-CONNECT proof: a fresh device uuid + session int in
    the captured structure yields CONNACK rc=0 — full structural control
    over the handshake without replaying captured bytes.

    Args:
        user_id: The operator's numeric c_user id.
        user_agent: Optional UA override (sanitized inside the builder).
        device_id: Optional device uuid; fresh uuid4 when None.
        session_id: Optional session int; fresh random when None.
        client_id: The MQTT client id bytes; defaults to the captured
            ``mqttwsclient`` prefix (docs/15 §P2-4).
        keepalive: Keepalive seconds; defaults to the captured 15s.

    Returns:
        The complete CONNECT packet bytes, ready for the socket.
    """
    identity = build_identity_json(user_id, user_agent=user_agent,
                                   device_id=device_id, session_id=session_id)
    username = json.dumps(identity, separators=(",", ":")).encode()
    return build_connect(username, client_id=client_id, keepalive=keepalive)


def rewrite_connect_identity(
    captured_connect: bytes,
    expected_uid: str,
) -> bytes:
    """Clone a captured CONNECT with a FRESH device uuid + session id.

    Strategy (P3-1's rebuilt-CONNECT proof, formalised):
      1. decode the identity JSON out of the packet's username field;
      2. swap device uuid (same length) and bump the session int;
      3. if the re-encoded username keeps the original length, splice it in
         place — otherwise rebuild the entire CONNECT from the identity
         (build_fb_connect always yields a valid packet).

    Args:
        captured_connect: The raw CONNECT bytes from the live capture.
        expected_uid: The uid the capture must carry — the operator's own
            c_user.

    Returns:
        The rewritten CONNECT packet bytes with fresh identity material.

    Raises:
        ValueError: When the capture's identity does not match the
            expected actor, carries no username to rewrite, or the device
            uuid length drifts — we never silently re-sign another
            session.
    """
    from .frames import decode_connect

    anatomy = decode_connect(captured_connect)
    if not anatomy["username"]:
        raise ValueError("captured CONNECT has no username to rewrite")
    identity = json.loads(anatomy["username"])
    if identity.get("u") != expected_uid:
        raise ValueError("captured identity uid does not match expectation")

    new_device = str(uuid.uuid4())
    if len(new_device) != len(identity["d"]):
        raise ValueError("device id length drift")
    identity["d"] = new_device
    identity["s"] = identity["s"] + 1

    new_username = json.dumps(identity, separators=(",", ":"))
    if len(new_username) == len(anatomy["username"]):
        return captured_connect.replace(
            anatomy["username"].encode(), new_username.encode(), 1)
    return build_fb_connect(identity["u"], user_agent=identity["a"],
                            device_id=identity["d"],
                            session_id=identity["s"])
