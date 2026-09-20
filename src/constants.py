"""Protocol constants — the single source of truth for every live-derived
value in the codebase (docs/15-live-calibration-findings.md, all phases).

Each constant below was confirmed against live wire capture (Phase 1-9
probes), harvested from production JS bundles (docs/13 §2), or pinned from
the protocol specifications in docs/02-06. Per the §6 standard every group
carries a section banner and every wire-format literal cites its calibration
source (docs/ section + Phase reference).

ARCHITECTURE:

  This module is deliberately dependency-free (stdlib-only, no I/O, no
  FBK_* environment reads) so every layer — transport, auth, graphql,
  realtime, surfaces, journal — can import it without violating the
  docs/12 §2 layering rule. It encodes two classes of value:

  * OPERATING constants — endpoints, cookie taxonomies, mutation doc_ids —
    consumed directly by the runtime surfaces;
  * DOCUMENTATION constants — wire shapes observed live or mapped during
    reconnaissance but not exercised by any code path (LOGOUT_ENDPOINT,
    TELEMETRY_BNZAI, AUTH_COOKIES, ...). They are kept as protocol memory:
    the live captures that validated them are not reproducible on demand,
    and each is annotated below with the feature it documents.

CALIBRATION NOTES:

  All bare ``P``-references (P2-3, P3-6, ...) point into the Phase addenda
  of docs/15-live-calibration-findings.md unless another document is named.
  Values flagged ``(verify)`` remain seed-knowledge from docs/03/04/14 and
  must not be treated as authoritative until a live probe pins them —
  docs/15 §4 records which error codes were never provoked.

SECURITY BOUNDARY:

  SECRET_COOKIE_NAMES / SECRET_TOKEN_NAMES are the input vocabulary of the
  journal redaction pipeline (docs/11 §7): any journal record keyed by one
  of these names must carry a salted sha256 fingerprint, never the raw
  value. Cookie and token VALUES never appear in this file or its comments.

USER-DOC ANCHOR: cli/docs/07-reference-tooling.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

# ── HTTP Hosts & Endpoint Map ───────────────────────────────────────────────
# Live-confirmed hosts and endpoint shapes (docs/15 §1, §5; docs/04 §1).
DOMAINS: dict[str, str] = {
    # Comet desktop/web surface — all bootstrap and GraphQL traffic (docs/04 §1).
    "www": "https://www.facebook.com",
    # Mobile surface: retired as a touch target — 302 → www ?_rdr (docs/15 §5).
    "m": "https://m.facebook.com",
    # Content-hashed JS chunk CDN — the doc_id harvest source (docs/13 §2.1).
    "static_cdn": "https://static.xx.fbcdn.net",
    # Public OAuth/Graph API plane — separate auth surface from the web
    # cookie session; tokenless root GETs are rejected with 400 (docs/03 §7,
    # docs/15 §5).
    "graph": "https://graph.facebook.com",
}
# Persisted-query transport (docs/04 §1): POST-only, x-www-form-urlencoded,
# cookie-session auth. A GET on this path is a no-op — 200 with a 0-byte
# body (docs/15 §5) — so read traffic must never fall back to GET.
GRAPHQL_ENDPOINT = "https://www.facebook.com/api/graphql/"
# DOCUMENTATION-ONLY (zero code references): the pre-Comet multi-query batch
# surface — ``q=0&batch0=...``, answered with newline-concatenated JSON and
# the ``for (;;);`` hijack guard (docs/04 §3.3). Comet uses the single-doc
# /api/graphql/ path; kept as protocol memory for the NDJSON heritage.
GRAPHQL_BATCH_ENDPOINT = "https://www.facebook.com/api/graphqlbatch/"
# DOCUMENTATION-ONLY (zero code references): the legacy Banzai telemetry
# batch endpoint. A live 40-second browser session produced ZERO /ajax/bz
# POSTs — web-client telemetry now rides WS-borne Pigeon batches, so
# synthetic-telemetry work must target the WS format, never this path
# (docs/15 §P3-4).
TELEMETRY_BZ = "https://www.facebook.com/ajax/bz"
# DOCUMENTATION-ONLY (zero code references): /bz's live successor endpoint —
# the telemetry-presence layer a deep behavioral model could notice fbk does
# not emit (docs/16 §1). fbk deliberately emits NO telemetry: fabricating
# organic-shaped presence events is out of policy (docs/16 §3-§4).
TELEMETRY_BNZAI = "https://www.facebook.com/ajax/bnzai"
# DOCUMENTATION-ONLY (zero code references): the Quality-Manager ping surface
# of the legacy /ajax family. No docs/ live capture pins its current shape —
# carried from endpoint-surface reconnaissance only (docs/02 family).
QM_ENDPOINT = "https://www.facebook.com/ajax/qm/"
# DOCUMENTATION-ONLY (zero code references): the checkpoint-aware async login
# POST target (docs/03 §2.2). fbk never logs in programmatically — the
# operator authenticates in a real browser and exports cookies.txt, because a
# login event is the highest-scrutiny moment on the surface (docs/11 §4).
LOGIN_ENDPOINT = "https://www.facebook.com/login/device-based/login/async/"
# DOCUMENTATION-ONLY (zero code references): logout.php pairs with the
# logout_hash the bootstrap already harvests from the viewer frame
# (docs/15 §2) — together they are the complete session-teardown POST pair
# should an operator ever need a clean server-side logout.
LOGOUT_ENDPOINT = "https://www.facebook.com/logout.php"

# ── TLS / h2 Impersonation Targets ──────────────────────────────────────────
# Transport identity is non-negotiable: the pinned, versioned target keeps
# the ClientHello byte-stable across library upgrades, while the unversioned
# "chrome" preset would silently rotate the fingerprint on a curl_cffi bump
# (docs/16 §6). chrome136 was confirmed live 2026-09 to pass the edge with
# zero interstitials across ~40 requests (docs/15 §1).
IMPERSONATE_DEFAULT = "chrome136"
# Descent order probed by transport.resolve_impersonate(); the first target
# the installed curl_cffi build can express wins (docs/12 §1.1 probe trick).
IMPERSONATE_FALLBACKS = ("chrome136", "chrome131", "chrome124", "chrome120")

# ── Cookie Taxonomy ──────────────────────────────────────────────────────────
# Live jar set (docs/15 §1): c_user, datr, dpr, fr, presence, ps_l, ps_n,
# sb, wd, xs. Semantics and lifetimes per docs/03 §1.1.

# DOCUMENTATION-ONLY (zero code references): the session-authentication pair.
# xs is the actual bearer credential; c_user is the uid claim it must agree
# with — the server validates the pair as a unit (docs/03 §1.1, §1.3).
AUTH_COOKIES = ("c_user", "xs")
# DOCUMENTATION-ONLY (zero code references): device/integrity cookies. datr
# is the pre-auth device anchor and the central bot-scoring input (docs/08);
# sb is the cross-product sub-browser id (docs/03 §1.1). Both accumulate
# reputation and must never be fabricated or rotated casually (docs/11 §9).
DEVICE_COOKIES = ("datr", "sb")
# DOCUMENTATION-ONLY (zero code references): browser-plausibility cookies —
# absence of wd/dpr/presence on an authenticated desktop session is a clean
# automation tell, so the full set ships on every request (docs/03 §9.1).
INFO_COOKIES = ("wd", "dpr", "presence", "fr", "ps_l", "ps_n", "locale")

# Redaction input (docs/11 §7): a journal entry keyed by one of these names
# must hold a salted sha256 fingerprint, never the raw value. Superset of
# the docs/12 §4.5 SECRET_COOKIE_RE — journal redaction is never weaker.
SECRET_COOKIE_NAMES = frozenset({"xs", "datr", "c_user", "sb", "fr", "presence"})
# Same contract for non-cookie secrets: fb_dtsg/lsd are the CSRF pair
# (docs/03 §4-§5); access_token is the OAuth-plane credential (docs/03 §7);
# sessionid is the Instagram-family session cookie (docs/03 §1.2); and
# privacy_write_id is the opaque renderer-scope id shipped in
# CometPrivacySelectorSavePrivacyMutation inputs (docs/15 §P3,
# surfaces/settings.py) — possession of the name never authorizes replay.
SECRET_TOKEN_NAMES = frozenset({"fb_dtsg", "lsd", "access_token", "sessionid",
                                "privacy_write_id"})

# ── Bootstrap Login-State Markers ────────────────────────────────────────────
# require-frame JSON markers scraped from served homepage HTML (docs/15 §2).
LOGGED_IN_MARKERS = ("CurrentUserInitialData", "DTSGInitData")
# Login-page DOM markers (docs/03 §3.1): any one of these at 200 = logged out.
LOGGED_OUT_MARKERS = ('id="login_form"', "login_form_row", 'name="login"', "RecaptchaDialogForm")
# Authoritative checkpoint signal, shipped inside the config frames
# (docs/15 §2, §P2-2). String-matching "/checkpoint" URLs false-positives
# on healthy pages — the string appears in routing tables — so the flag is
# the only classifier input; do not downgrade to URL matching.
CHECKPOINT_FLAG = '"is_checkpointed":true'

# ── Account-Level Enforcement Banners ────────────────────────────────────────
# The "automated behaviour" enforcement ladder endpoint (docs/15 §P8-1 /
# §P9-2): the Phase-8 session kill escalated to an account-level warning.
# bootstrap_homepage scans every served page for these markers so the
# operator sees the enforcement state without waiting for an email; surfaces
# must DETECT them and refuse to push further (docs/10 §7 containment).
ACCOUNT_WARNING_MARKERS = (
    "We suspect automated behaviour",
    "suspicious activity on your account",
    "temporarily restricted",
    "permanently disabled",
    "unusual activity",
)

# ── GraphQL Error Envelope Codes ─────────────────────────────────────────────
# /api/graphql/ returns HTTP 200 for nearly all logical errors; the typed
# failure rides in errors[].code (docs/04 §3.4, docs/14 §3). 1675012 is the
# only live-provoked code (docs/15 §4); every (verify) row is seed-knowledge
# the classifier must not treat as authoritative until a probe pins it.
GRAPHQL_ERROR_CODES: dict[int, str] = {
    1357004: "NOT_LOGGED_IN",        # docs/03 §3.1 — canonical logged-out signal, high confidence
    1675012: "VARIABLE_COERCION",    # live-confirmed (docs/15 §4): missing_ /
                                     #   noncoercible_variable_value
    1357046: "RATE_LIMITED_SUSPECTED",  # (verify) — docs/04 §3.4 throttle family
    1357051: "SESSION_EXPIRED_DTSG",    # (verify) — docs/03 §4.3, docs/14 §3
    1677047: "INVALID_DTSG",           # (verify) — docs/04 §3.4 dtsg/param-invalid family
    1570245: "DOC_ID_UNKNOWN",         # (verify) — docs/04 §3.4 stale persisted-query family
    1384000: "CHECKPOINT_REQUIRED",     # (verify) — docs/03 §3.2 checkpoint/challenge family
}
# Anti-JSON-hijacking guard prefixing legacy ajax/GraphQL responses
# (docs/14 §1.3): exactly 9 bytes, stripped before parsing when present
# (docs/12 §6 parser contract).
LEGACY_JSONP_PREFIX = "for (;;);"

# ── UFI Reaction Surface ─────────────────────────────────────────────────────
# Full reaction enum, harvested live 2026-09 from the CometUFIFunnelLogger
# switch in bundle ExkZXZ_lB5E.js (docs/15 §P3-2). Passed as
# input.feedback_reaction_id in CometUFIFeedbackReactMutation (docs/04 §6).
UFI_REACTION_IDS: dict[str, str] = {
    "LIKE": "1635855486666999",
    "LOVE": "1678524932434102",
    "WOW": "478547315650144",
    "HAHA": "115940658764963",
    "SORRY": "908563459236466",  # displayed as "Care" in the 2024+ UI
    "ANGER": "444813342392137",
    "SUPPORT": "613557422527858",
    # removes any existing reaction — same mutation, no separate unlike
    # pair (docs/15 §P2-3)
    "REMOVE": "0",
}
# One mutation covers set AND remove: the live replay cycled reaction_count
# 43 → 44 → 43 with reaction_id "1635855486666999" then "0" (docs/15 §P2-3).
# The friendly name lives on as the KNOWN_MUTATIONS key below and the
# surfaces' REACT_MUTATION literals — no third copy as a standalone constant.
UFI_REACT_DOC_ID = "27646120298312844"  # docs/15 §P2-3

# ── Composer Privacy Base States ─────────────────────────────────────────────
# audience.privacy.base_state wire literals, live-captured from the
# ComposerStoryCreateMutation / CometPrivacySelectorSavePrivacyMutation
# captures (docs/15 §P3). The CLI-facing name (PUBLIC/FRIENDS/PRIVATE) maps
# onto the server's enum here; the wire never sees the CLI spelling.
PRIVACY_BASE_STATES: dict[str, str] = {
    "PUBLIC": "EVERYONE",
    "FRIENDS": "FRIENDS",
    "PRIVATE": "SELF",
}

# ── Confirmed Mutation Catalog ────────────────────────────────────────────────
# friendly_name → doc_id pairs from the harvested registry (docs/13 §2,
# assets/doc_id_registry*.json). Verification depth is source-tagged: the
# first block was replayed live end-to-end; the second is bundle-harvested
# (registry v2) and schema-derived — verified as registered operations, not
# as exercised calls.
KNOWN_MUTATIONS: dict[str, str] = {
    # live-verified end-to-end (docs/15 §P2-3, §P3-3, §P5-1):
    "CometUFIFeedbackReactMutation": UFI_REACT_DOC_ID,
    "useCometUFICreateCommentMutation": "39607465588840384",  # docs/15 §3, §P3-3;
    # ROTATED with deploy revision 1047963790 (fresh full harvest
    # 2026-09-20; the old 28882491998025365 registration returned
    # field_exception 1357010 live - the audit's doc-id-mismatch class)
    "CometUFIDeleteCommentMutation": "28058620387108821",  # docs/15 §P3-3 (strict arg set)
    # the "X is typing…" broadcast pair (docs/15 §P3-3):
    "CometUFILiveTypingBroadcastMutation_StartMutation": "9815271091886179",
    "CometUFILiveTypingBroadcastMutation_StopMutation": "9972315006159780",
    "ComposerStoryCreateMutation": "28778531428503134",  # the publish path (docs/15 §P5-1)
    # settings privacy save (docs/15 §P3):
    "CometPrivacySelectorSavePrivacyMutation": "27802157519437974",
    # harvested from live bundles (registry v2), schema-derived:
    "useGroupsCometCreateMutation": "37309644325300927",
    "useGroupAddMembersMutation": "26949495548074408",
    "AdditionalProfilePlusCreationMutation": "23863457623296585",
    "CometPageLikeCommitMutation": "9647968328590344",
    "CometPageFollowCommitMutation": "29690201327260308",
    "useCometAIHTSSendMessageMutation": "28137996599166900",
    "useCometAIHTSSendMessageV2Mutation": "38081592568123136",
    "CometPasswordReauthenticationMutation": "28435732132699112",
    "useFXSettingsSendPasswordResetLinkForViewerMutation": "23901062569500160",
    # the live-verified canary query (docs/15 §3):
    "CometNotificationsBadgeCountQuery": "9714526941947209",
    # stories surface, bundle-decoded 2026-09-20 from the /stories/create/
    # composer chunk graph (LocalArguments + transformer pipeline; the
    # create mutation carries the full SATP/photo/video input):
    "StoriesCreateMutation": "26770527039211553",
    # story reply (the viewer sheet's send-reply commit, same chunk family):
    "useStoriesSendReplyMutation": "27836479672650087",
    # story viewers list (StoriesSuspenseViewerSheetViewerListV2Query,
    # args {cursor, id, viewerCount}):
    "StoriesSuspenseViewerSheetViewerListV2Query": "38550750487903492",
    # the story 3-dot menu (StoriesSuspenseCardOptionMenuExperimental —
    # response carries the per-option action data incl. DELETE):
    "StoriesSuspenseCardOptionMenuExperimentalWithEntryPointQuery": "38364108549904404",
    # caret-menu NFX pair (the feed-story menu framework's execute/undo
    # commits; input schema decoded per docs/15 live probes):
    "CometFeedStoryExecuteNFXActionMutation": "28746494401615167",
    "CometFeedStoryUndoNFXActionMutation": "27602007642724569",
}

# ── Messenger Realtime: MQTT over WebSocket (legacy variant) ─────────────────
# Live 2026-09 (docs/15 §P2-4, §P3-1): edge.facebook.com is NXDOMAIN — the
# broker has migrated to edge-chat.facebook.com; every cached URL referencing
# the old host fails immediately.
# ?region=cln&sid=<id>&cid=<uuid> (docs/15 §P2-4a)
MESSENGER_MQTT_WS = "wss://edge-chat.facebook.com/chat"
# MQIsdp level-3 dialect — NOT standard MQTT v3.1.1 (docs/06 correction per
# docs/15 §P2-4a): a standard 3.1.1 CONNECT is parsed and gracefully refused
# with a WS 1000 close.
MQTT_PROTOCOL_NAME = "MQIsdp"
MQTT_PROTOCOL_LEVEL = 3
MQTT_CONNECT_FLAGS = 0x82        # username flag + clean session (docs/15 §P3-1 CONNECT anatomy)
MQTT_KEEPALIVE_S = 15            # seconds; PINGREQ/PINGRESP cycle confirmed live (docs/15 §P2-4a)
MQTT_CLIENT_ID_PREFIX = "mqttwsclient"  # CONNECT payload client-id prefix (docs/15 §P3-1)
MQTT_TOPICS = {
    # classic Mercury delta bus — DEAD for chats since E2EE (docs/15 §P7-1)
    "main_sync": "/t_ms",
    # live-observed first subscribe of the web client (docs/15 §P3-1)
    "rtc_multi": "/t_rtc_multi",
    "rtc": "/t_rtc",
    "presence": "/$pc",   # presence subscribe — spelling (verify) per docs/06 topic table
    "typing": "$typ",     # spelling varies across writeups (docs/06)
}
# MQTT fixed-header opcodes used by the codec (docs/06 §3 framing).
MQTT_OP_CONNECT, MQTT_OP_CONNACK = 0x10, 0x20
MQTT_OP_PUBLISH, MQTT_OP_PUBACK = 0x30, 0x40
MQTT_OP_SUBSCRIBE, MQTT_OP_SUBACK = 0x82, 0x90
MQTT_OP_PINGREQ, MQTT_OP_PINGRESP = 0xC0, 0xD0
MQTT_OP_DISCONNECT = 0xE0

# ── Messenger Realtime: DGW (data-gateway GraphQL-over-WS) ───────────────────
# All four channels replayed verbatim on fresh sockets with cookies as the
# only credential material, each accepted (docs/15 §P3-6).
MESSENGER_WEB_APP_ID = "2220391788200892"  # Messenger web app id — x-dgw-appid (docs/15 §P2-4b)
MESSENGER_DGW_WS = {
    "rpsignaling": "wss://gateway.facebook.com/ws/rpsignaling",
    "realtime": "wss://gateway.facebook.com/ws/realtime",
    "lightspeed": "wss://gateway.facebook.com/ws/lightspeed",
    "streamcontroller": "wss://gateway.facebook.com/ws/streamcontroller",
}
# Connection query params replayed verbatim without rejection (docs/15
# §P3-6); x-dgw-uuid=<c_user>, x-dgw-deviceid and x-dgw-loggingid are
# per-socket and supplied by the realtime clients, not baked here.
DGW_QUERY_PARAMS = {
    "x-dgw-appid": MESSENGER_WEB_APP_ID,
    "x-dgw-appversion": "0",
    "x-dgw-authtype": "1:0",
    "x-dgw-version": "5",
    "x-dgw-tier": "prod",
    "x-dgw-app-stream-group": "group1",
}
# DGW frame opcodes (docs/15 §P3-6). Fixed-header layout:
#   byte0 = opcode | seq(2B LE) | len(2B LE) | flags(1B) | payload.
DGW_OP_CONTROL = 0x0F   # JSON control frames; handshake ack {"code":200} (docs/15 §P3-6)
# task envelopes, prefix \x00\x80; GraphQL {"input":...} rides the
# streamcontroller socket, not lightspeed (docs/15 §P6-2):
DGW_OP_DATA = 0x0D
DGW_OP_ACK8 = 0x0C      # 8-byte ack echoing the seq; payload \x00\x00 (docs/15 §P6-2)
DGW_OP_ACK3 = 0x0E      # 3-byte per-stream ack: op+seq, no header (docs/15 §P6-2)
# 1-byte pings, confirmed live on all channels (docs/15 §P2-4b):
DGW_OP_PING, DGW_OP_PONG = 0x09, 0x0A
DGW_SERVER_FIRST_BYTE = 0x0A   # server-first byte on connect — read then discarded (docs/15 §P3-6)

# ── Behavioral Detection Heuristics (inter-arrival pacing verdicts) ────────────
# docs/10 heuristics (§4, §8 symptom catalogue): a p95 inter-arrival below
# BURST_P95_GAP_S is a burst signature; a mean gap above SPARSE_MEAN_GAP_S is
# anomalously sparse. Both consumers — the measurement surface's report()
# verdict (docs/10 §8) and the journal-driven pacing audit (journal/analysis,
# docs/11 §8) — must share the EXACT same thresholds, so they live here, the
# single source of truth for protocol constants (§6 standard): surfaces/
# measurement.py and journal/analysis.py import them from this module.
BURST_P95_GAP_S = 0.25    # docs/10 §4: the machine-precision burst window
SPARSE_MEAN_GAP_S = 60.0  # docs/10 §8: the anomalously-sparse signature
