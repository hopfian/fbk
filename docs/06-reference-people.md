# 06 — People & social reference

The complete command reference for the five people-and-social families:
`messenger` (threads, history, send, listen), `friends` (list, requests,
suggestions, and the five friending verbs plus the badge clear), `groups`
(create, join, add-members, request-join, feed, post, members), `pages`
(create, like, follow, feed), and `settings` (show, set-default-privacy,
verify-password, send-reset-link). Every flag below was taken from
`fbk <family> <sub> --help` and the implementing source; every wire claim
cites its research doc by filename from the repo-root suite
(`docs/01`–`docs/16`), which is the ground truth for protocol internals.

Conventions used throughout:

* **Global flags are not repeated here.** Every (sub)command also accepts
  `--root`, `--cookies`, `--no-journal`, `--json`, `--retry N`, and
  `--dry-run` — see [03-session-and-auth.md](03-session-and-auth.md) §8.
  Flag names are typed in full; prefix abbreviation is disabled tree-wide.
* **Human output always trails one compact JSON line** of the same payload,
  so scripts can pipe the last line regardless of mode
  ([03-session-and-auth.md](03-session-and-auth.md) §8).
* **Mutations consume the separate 40/day budget**, not the 500/day read
  cap — the governor enforces both. Read the budget policy in
  [09-safety-and-opsec.md](09-safety-and-opsec.md) before firing any
  write, and expect the governor to pace every request
  ([01-getting-started.md](01-getting-started.md)).
* **Exit codes** follow the shared contract in
  [03-session-and-auth.md](03-session-and-auth.md) §8 /
  [11-troubleshooting.md](11-troubleshooting.md): a handler-returned 1 is
  a failed precondition (typically an unconfirmed mutation), never a
  crash; a refused realtime handshake is an unexpected exception → exit 2.

## Contents

* messenger — [threads](#fbk-messenger-threads) ·
  [history](#fbk-messenger-history) · [send](#fbk-messenger-send) ·
  [listen](#fbk-messenger-listen) (realtime bus primer inside)
* friends — [list](#fbk-friends-list) ·
  [requests](#fbk-friends-requests) ·
  [suggestions](#fbk-friends-suggestions) ·
  [request](#fbk-friends-request) · [cancel](#fbk-friends-cancel) ·
  [accept](#fbk-friends-accept) · [decline](#fbk-friends-decline) ·
  [unfriend](#fbk-friends-unfriend) ·
  [clear-badge](#fbk-friends-clear-badge)
* groups — [create](#fbk-groups-create) · [join](#fbk-groups-join) ·
  [add-members](#fbk-groups-add-members) ·
  [request-join](#fbk-groups-request-join) · [feed](#fbk-groups-feed) ·
  [post](#fbk-groups-post) · [members](#fbk-groups-members)
* pages — [create](#fbk-pages-create) · [like](#fbk-pages-like) ·
  [follow](#fbk-pages-follow) · [feed](#fbk-pages-feed)
* settings — [show](#fbk-settings-show) ·
  [set-default-privacy](#fbk-settings-set-default-privacy) ·
  [verify-password](#fbk-settings-verify-password) ·
  [send-reset-link](#fbk-settings-send-reset-link)

---

## messenger

The Messenger surface has an honest contract, live-calibrated 2026-09
(research doc: docs/15-live-calibration-findings.md §P7): **threads and
history read through the GraphQL state bridges; the plaintext send plane
reaches only the self/AI chat.** Person-to-person messages are end-to-end
encrypted and route through client-side crypto that cannot be replayed
from a plaintext wire — a plaintext send into a P2P thread *silently
misroutes* (verified: thread snippet unchanged). Reads ride the GraphQL
message-range family (research doc: docs/02-endpoint-surface-map.md §2.2).
Message bodies are PII: human output prints whitespace-collapsed **heads
only** (24 chars, ellipsis-marked); full text stays in `--json` and the
journal, where the operator's own redaction policy applies (research doc:
docs/11-opsec-and-session-engineering.md §7).

## fbk messenger threads

List recent message threads, newest first. This is where you get the
thread keys every other messenger subcommand takes.

```
fbk messenger threads [--limit N]
```

| Flag | Default | Semantics |
|---|---|---|
| `--limit N` | 20 | Maximum threads to return (applied after the walk) |

```
$ fbk messenger threads
123456789012345  Example User  [2 participants]  see you at the demo…
12345678901234   (unnamed)     [1 participants]  note to self… self-thread
{"threads": [...], ...}
```

Output notes: each human line carries the **thread key** (the handle
`history`/`send` take), a sanitized name head, the **participant count**,
and a snippet head. Self-threads — the participant list is exactly the
viewer — are marked `self-thread`; they are the only E2EE-safe send
targets. There is **no unread counter**: the row model is id, name,
snippet, participant ids, `is_self_thread` — nothing else. `--json`
carries the full rows.

The service replays `MWCMBlendedThreadListQuery` (the thread-list page's
QP query, preloaded with empty variables by the real client) and, when
the current build returns no thread rows on that query — the 2026-09
behavior — falls back to the **Lightspeed state bridge**
(`LSPlatformGraphQLLightspeedRequestQuery`), the same e2ee thread-snapshot
sync the /messages/ inbox rides (research doc:
docs/15-live-calibration-findings.md §P2-4).

Live status: live-verified end to end 2026-09 (QP contract replay plus the
Lightspeed fallback path).

## fbk messenger history

Read one thread's recent messages, newest first — the encrypted-backup
(EB) message-range read.

```
fbk messenger history --thread-id THREAD_ID [--limit N]
```

| Flag | Default | Semantics |
|---|---|---|
| `--thread-id ID` | required | Target thread key from `messenger threads`; raw numeric or b64 `Thread:<n>` global id — both are normalized |
| `--limit N` | 20 | Maximum messages; rides the wire as `query_num_messages` AND is re-applied as a local post-slice |

```
$ fbk messenger history --thread-id 123456789012345
12345678901234 | ping when the build lands | 2026-09-20T10:14:03+00:00
{"thread_id": "123456789012345", "messages": [...]}
```

Output notes: each line prints sender (name or id), text head, and an
ISO-8601 UTC timestamp. Encrypted stanzas carry no plaintext field on the
wire — they render as `(no text)` in human mode.

Wire semantics (from the surface's live probing, research doc:
docs/15-live-calibration-findings.md §P7-2): the read replays
`EBMessageRangeQueryForThreadsQuery`, the EB message-range query the web
client uses for thread history, with one `restore_payload_strings` range
descriptor: `BEFORE` direction against a reference timestamp of *now* (the
most recent window), `query_num_messages` = limit, and the thread key as
the Int64 `server_thread_key`. The `raw_tokens` of the request are
per-device e2ee enrollment crypto that cannot be fabricated; without the
device's real enrollment, the endpoint answers one deterministic
`field_exception` with an **empty row set** — the command maps that
protocol-level gate to an empty history (exit 0; typed
session/checkpoint/rate errors still propagate with their own codes).
An empty result on a P2P thread is therefore expected account state, not
a failure.

Live status: live-verified read (query schema live-probed 2026-09, six
name-guided attempts); P2P history is cryptographically gated — expect
empty rows without device enrollment.

## fbk messenger send

Send one text message to one thread. **This is the mutation** — it
consumes the 40/day mutation budget
([09-safety-and-opsec.md](09-safety-and-opsec.md)).

```
fbk messenger send THREAD_ID --text TEXT [--force]
```

| Argument / flag | Default | Semantics |
|---|---|---|
| `THREAD_ID` (positional) | required | Target thread key from `messenger threads` |
| `--text TEXT` | required | Message body; a plain string on the wire, NOT the `{ranges, text}` object other composer mutations use |
| `--force` | off | Override the E2EE guard — see below |

```
$ fbk messenger send 12345678901234 --text "self note"
send to 12345678901234: OK
{"thread_id": "12345678901234", "sent": true}
```

The E2EE guard: the only wire-verified plaintext send plane is the
**self/AI chat** (`useCometAIHTSSendMessageMutation`, with the V2 variant
as protocol-level fallback). A self-addressed send — thread id equal to
your own `c_user` — lands in your own chat. Person-to-person threads are
E2EE; plaintext attempts **silently misroute** (research doc:
docs/15-live-calibration-findings.md §P7-3), so the command refuses any
target that is not the viewer unless `--force` acknowledges the misroute
risk explicitly. Use `--force` only for protocol experiments.

Output notes: `OK` when the response echoes message rows; `no message
rows echoed` otherwise. **Exit 0 only on a confirmed send** — an
unconfirmed mutation is a failed precondition (exit 1), never counted as
sent. Soft suppression is invisible in-band: check the echoed rows, not
the success envelope (research doc:
docs/10-rate-limiting-and-behavioral-detection.md §3.4).

Live status: live-verified for the self/AI chat (docs/15 §P7); P2P sends
are refused by design.

## fbk messenger listen

Subscribe to the realtime bus and collect frames for a fixed window.
`listen` is an observation command, not a mutation — but it opens live
authenticated sockets, so treat it as wire-touching when planning.

```
fbk messenger listen [--dgw] [--seconds S] [--topics LIST]
```

| Flag | Default | Semantics |
|---|---|---|
| `--dgw` | off | Listen on the DGW lightspeed socket instead of the MQTT bus |
| `--seconds S` | 30.0 | Collection window; the socket closes at the deadline regardless of pending frames |
| `--topics LIST` | `/t_ms,/t_rtc_multi` | Comma-separated MQTT topic list (MQTT mode only) |

```
$ fbk messenger listen --seconds 10
[1] PUBLISH /t_ms 214B 1,7,14
[2] PUBLISH /t_rtc_multi 58B 1
{"frames": [...]}

$ fbk messenger listen --dgw --seconds 10
[1] rid=1 state_sync_result {"database":95,"epoch_id":0,"las…
{"frames": [...]}
```

### How the realtime bus works

**MQTT mode (default).** The client opens a fingerprint-coherent
WebSocket to `wss://edge-chat.facebook.com/chat` — the chrome136 TLS/h2
impersonation and the session cookies ride the WS handshake itself, so
the account presents one TLS identity on every wire contact (research
doc: docs/16-client-realism-and-detection-landscape.md §2). The broker
speaks a Facebook dialect of MQTT, not the OASIS spec: protocol name
`MQIsdp`, level 3, and the CONNECT `username` field carries a plain-UTF-8
JSON identity blob (user-agent, app id, device uuid, session int, uid)
rather than a credential — a vanilla MQTT 3.1.1 CONNECT is refused
outright. The verified flow: server-first `0x0A` byte, client CONNECT
(fresh identity built per run), CONNACK `rc=0` (any non-zero return code
is fatal for the window), then SUBSCRIBE of `/t_ms` and `/t_rtc_multi`
with SUBACK enforcement — a `0x80` grant is a hard error. A 15-second
keepalive is negotiated and the client pings on a 10-second send-side
cadence, including during inbound traffic. PUBLISH delta bodies on
`/t_ms` are Thrift-compact binary; fbk decodes them generically into a
field-id-keyed summary without needing the Messenger .thrift IDL
(research doc: docs/06-messenger-mqtt-protocol.md §3–§5, §8).

**`--dgw` mode.** The alternative transport is the DGW
(data-gateway) lightspeed socket at
`wss://gateway.facebook.com/ws/lightspeed`, carrying the state-sync
dialect the modern Messenger inbox actually rides. Frames are a fixed
6-byte header (`opcode | seq 2B LE | len 2B LE | flags`) with JSON
payloads: `0x0F` control handshake (expect `{"code":200}`), `0x0D` data
(request/response), `0x0C/0x0E` acks, `0x09/0x0A` pings. The client
connects with a fresh device uuid, sends the captured
`client_subscribe` for the thread list plus the live-captured default
thread, and auto-acks every received data frame. Responses echo the
request id (`rid`), carry a `payload_type` from the catalog
(`client_subscribe` / `state_sync` / `state_sync_delta` /
`state_sync_result`), and print as a decoded JSON head (research docs:
docs/15-live-calibration-findings.md §P3-6–§P3-7, §P6-2).

**What to expect, honestly:** the classic `/t_ms` bus is **dead for chat
delivery** — message sync rides the DGW lightspeed channel, and the
captured production client only ever subscribed `/t_rtc_multi` (call
signaling) on the MQTT socket (research doc:
docs/15-live-calibration-findings.md §P7-1). No `/t_ms` PUBLISH delta has
been captured yet; a silent MQTT window (zero frames, exit 0) is a valid
observation, not a connection failure. The DGW path is where live
state-sync responses were confirmed.

**Requirements.** The primary WebSocket transport is `curl_cffi` (a core
dependency) with browser-impersonated TLS. The `websockets` library is
only the deliberate fallback for curl_cffi builds without sync WS
support — it is not installed by the base install, so if your build hits
the fallback gate, install it with `pip install -e .[realtime]` (or
`.[dev]`, which includes it). Nothing else is needed; a genuinely
refused handshake (CONNACK non-zero, missing `{"code":200}`, rejected
SUBACK) surfaces as an error with exit 2.

**Teardown.** The window always ends by the `--seconds` deadline and the
socket is closed idempotently. `Ctrl-C` interrupts early: the socket is
torn down in the same cleanup path and the process exits 130 with no
traceback ([03-session-and-auth.md](03-session-and-auth.md) §8).

Live status: both handshakes live-verified 2026-09 — MQTT CONNECT (fresh
identity and verbatim capture replay both yield CONNACK rc=0; research
doc: docs/15-live-calibration-findings.md §P3-1) and the DGW `{"code":200}`
handshake with state-sync round-trips (§P6-2). No `/t_ms` delta has been
captured yet — the first real delta capture is an operator-gated event
(research doc: docs/15-live-calibration-findings.md §P5-2).

---

## friends

The friending family (research doc: docs/02-endpoint-surface-map.md §2.9).
Every read — `list`, `requests`, `suggestions` — replays **one** root
payload: `FriendingCometRootContentQuery`, the query the /friends/ page
preloads with verbatim `{"scale": 2}` variables, whose selection carries
friends, requests, suggestions, and counts in a single response. The
mutation verbs ship schema-decoded templates; most mint a fresh
`click_correlation_id` (uuid4) per call — the per-call nonce this family
uses in place of `client_mutation_id`.

**Id semantics — read this before firing any verb.** Every verb takes a
**user id**, never a separate "request id": request rows are keyed by the
counterparty's user id. `request` and `cancel` take the id of the *user
you targeted*; `accept` and `decline` take the id of the *user who sent
you* the request (from `requests --direction incoming`); `unfriend`
takes the friend's id from `list`.

## fbk friends list

Friend rows from the friends-page root query: the
`friend_confirmed_notifications` edges (recently confirmed friends) plus
every `User` node whose viewer-relative `friendship_status` is
`ARE_FRIENDS`. Request rows and suggestions stay out.

```
fbk friends list [--limit N]
```

| Flag | Default | Semantics |
|---|---|---|
| `--limit N` | 50 | Maximum friend rows to emit (applied after the walk) |

```
$ fbk friends list
friends: 2
Example User 12345678901234567
Another User 98765432109876543
{"friends": [...], "count": 2}
```

Live status: live-verified — the root query replayed verbatim against the
/friends/ page 2026-09.

## fbk friends requests

Incoming and outgoing friend requests, split from the same root payload's
`friend_requests` connection (the `friending_possibilities` rows whose
viewer-relative `friendship_status` is `INCOMING_REQUEST` or
`OUTGOING_REQUEST`).

```
fbk friends requests [--direction {incoming,outgoing,all}]
```

| Flag | Default | Semantics |
|---|---|---|
| `--direction` | `all` | Which side to populate; the dropped side emits `[]`. The `--json` payload always names both sides |

```
$ fbk friends requests
incoming: 1
Example User 12345678901234567
outgoing: 0
{"incoming": [...], "outgoing": []}
```

Output notes: rows carry `{id, name, friendship_status, [expiration_time]}`
— the id is the **counterparty's user id** (the sender's id on incoming
rows; the requestee's id on outgoing rows), which is exactly what
`accept`/`decline` and `cancel` take. `expiration_time` is an edge-level
field when the server includes it.

Live status: live-verified — same verbatim root replay, 2026-09.

## fbk friends suggestions

People-you-may-know rows — the root payload's `pymk_grid` connection
(alias for `people_you_may_know(first:20, location:"FRIENDS_HOME_MAIN")`).
Suggestions are not friends and carry no friendship-status requirement;
rows without a name are skipped.

```
fbk friends suggestions [--limit N]
```

| Flag | Default | Semantics |
|---|---|---|
| `--limit N` | 10 | Maximum suggestion rows (applied after dedup on id) |

```
$ fbk friends suggestions
suggestions: 1
Example User 12345678901234567
{"suggestions": [...], "count": 1}
```

Live status: live-verified — the pymk_grid connection confirmed in the
2026-09 /friends/ preload.

## fbk friends request

Send a friend request (`FriendingCometFriendRequestSendMutation`). The
id lands in `input.friend_requestee_ids`; a fresh `click_correlation_id`
is minted per call. **Mutation** — 40/day budget.

```
fbk friends request --user-id ID
```

| Flag | Default | Semantics |
|---|---|---|
| `--user-id ID` | required | Target user id (numeric fbid) — the person you are requesting |

```
$ fbk friends request --user-id 12345678901234567
FriendingCometFriendRequestSendMutation: ['friend_request_send']
```

Friending mutations live in the classic per-(uid, target) anti-stalking
buckets (research doc: docs/10-rate-limiting-and-behavioral-detection.md
§1): request velocity toward many fresh targets is the shape Facebook's
coordination detection was built around — acceptance-rate history weighs
as heavily as raw count. Keep volumes human-scale.

Live status: schema-decoded 2026-09 from the owning bundle; **not
live-fired** — the mutations are socially visible (research doc:
docs/15-live-calibration-findings.md §P4-2).

## fbk friends cancel

Cancel your own outgoing friend request
(`FriendingCometFriendRequestCancelMutation`) — the low-risk retraction
path before a request ages. The id lands in
`input.cancelled_friend_requestee_id`. **Mutation** — 40/day budget.

```
fbk friends cancel --user-id ID
```

| Flag | Default | Semantics |
|---|---|---|
| `--user-id ID` | required | The user id your pending request targets (from `requests --direction outgoing`) |

```
$ fbk friends cancel --user-id 12345678901234567
FriendingCometFriendRequestCancelMutation: ['friend_request_cancel']
```

Live status: schema-decoded 2026-09; not live-fired.

## fbk friends accept

Accept an incoming friend request
(`FriendingCometFriendRequestConfirmMutation`). The id — the
**sender's** user id — lands in `input.friend_requester_id`. **Mutation**
— 40/day budget.

```
fbk friends accept --user-id ID
```

| Flag | Default | Semantics |
|---|---|---|
| `--user-id ID` | required | The requesting user's id (from `requests --direction incoming`) |

```
$ fbk friends accept --user-id 12345678901234567
FriendingCometFriendRequestConfirmMutation: ['friend_request_accept']
```

Live status: schema-decoded 2026-09; not live-fired.

## fbk friends decline

Decline (delete) an incoming friend request
(`FriendingCometFriendRequestDeleteMutation`). Same id semantics as
`accept`: the sender's user id in `input.friend_requester_id`.
**Mutation** — 40/day budget.

```
fbk friends decline --user-id ID
```

| Flag | Default | Semantics |
|---|---|---|
| `--user-id ID` | required | The requesting user's id (from `requests --direction incoming`) |

```
$ fbk friends decline --user-id 12345678901234567
FriendingCometFriendRequestDeleteMutation: ['friend_request_delete']
```

Live status: schema-decoded 2026-09; not live-fired.

## fbk friends unfriend

Remove an existing friend (`FriendingCometUnfriendMutation`). The id
lands in `input.unfriended_user_id` with the schema-decoded
`friends_manage_list` channel. **Mutation** — 40/day budget.

```
fbk friends unfriend --user-id ID
```

| Flag | Default | Semantics |
|---|---|---|
| `--user-id ID` | required | The friend's id (from `list`) |

```
$ fbk friends unfriend --user-id 12345678901234567
FriendingCometUnfriendMutation: ['friend_remove']
```

Unlike a declined request, an unfriend is a durable graph change — it
is not silently reversible.

Live status: schema-decoded 2026-09; not live-fired.

## fbk friends clear-badge

Clear the friends-nav badge counter
(`FriendingCometFriendsBadgeCountClearMutation`) — the exact write the UI
fires on opening the friends nav. No user input; the schema-decoded
friends-page commit variables ride verbatim. **Mutation** — 40/day
budget.

```
fbk friends clear-badge
```

```
$ fbk friends clear-badge
FriendingCometFriendsBadgeCountClearMutation: ['viewer_friends_badge_count_clear']
```

Live status: schema-decoded 2026-09; not live-fired.

**Output note for all six verbs:** mutation responses are emitted under a
one-level trim — dict values reduce to their key sets, lists to length
fingerprints — enough to confirm which fields came back without dumping
PII. The human line prints the mutation name and the sorted top-level
`data` keys, or `no data`. Exit is 0 on the in-band echo alone: friending
soft-failures are invisible in-band (research doc:
docs/10-rate-limiting-and-behavioral-detection.md §3.4) — "no data" is
the observable signature, never a confirmed effect. Verify with the read
commands.

---

## groups

The groups family (research doc: docs/02-endpoint-surface-map.md §2.7).
Feed and members reads follow the any-surface-page law: GET the group
page, harvest the SSR preload registration, replay it with the group id
substituted as the top-level `groupID` variable. Mutations ship
schema-decoded 2026-09 templates from the owning bundles, with a fresh
`client_mutation_id` and a minified-product attribution string per call.
Group posting reuses the shared composer mutation with the group context
attached — the same mutation the group UI fires (see
[04-reference-feed.md](04-reference-feed.md) for the composer family).

## fbk groups create

Create a new group (`useGroupsCometCreateMutation`).

```
fbk groups create --name NAME [--visibility {public,private}]
                  [--description TEXT] [--member-ids LIST]
```

| Flag | Default | Semantics |
|---|---|---|
| `--name NAME` | required | Group name; rides `input.name` |
| `--visibility` | `private` | Group privacy; the CLI spelling is uppercased to the wire enum (`PUBLIC`/`PRIVATE`) |
| `--description TEXT` | none | **Accepted but never serialized** — the decoded 2026-09 create input carries no description field; the flag exists for CLI symmetry and does not reach the wire |
| `--member-ids LIST` | none | Comma-separated user ids to invite in the same mutation (seeds both `members` and `bulk_invitee_members`) |

```
$ fbk groups create --name "Research notes"
group created: 12345678901234567
```

Output notes: the emitted payload is trimmed to the response's top-level
`data` keys with id-ish fields kept (huge Relay payloads never hit the
terminal); the human line prints the first id-shaped value found.

Live status: schema-decoded 2026-09 (bundle-verified input); live firing
is the operator's call — research doc: docs/11-opsec-and-session-engineering.md
§2.

## fbk groups join

Join a forum-style group (`GroupCometJoinForumMutation`, decoded wrapper
defaults: `feedType` DISCUSSION, `renderLocation` group_mall).

```
fbk groups join --group-id ID
```

| Flag | Default | Semantics |
|---|---|---|
| `--group-id ID` | required | Numeric group id; rides both the top-level `groupID` and `input.group_id` |

```
$ fbk groups join --group-id 12345678901234567
join requested for group 12345678901234567: ok
```

Output notes: in-band echo only. Gated groups answer with the request
flow instead — the payload distinguishes which path fired; use
`request-join` for those.

Live status: schema-decoded 2026-09; live firing is the operator's call.

## fbk groups add-members

Invite members to a group (`useGroupAddMembersMutation` →
`group_add_member`) — **invite semantics**: the ids are invitation
targets, and the emitted payload reports acceptance, not delivery.

```
fbk groups add-members --group-id ID --member-ids LIST
```

| Flag | Default | Semantics |
|---|---|---|
| `--group-id ID` | required | Numeric group id |
| `--member-ids LIST` | required | Comma-separated user ids to invite; ride `input.user_ids` |

```
$ fbk groups add-members --group-id 12345678901234567 --member-ids 12345678901234567,98765432109876543
invited 2 member(s) to group 12345678901234567
```

Invitations draw on the per-(uid, target) anti-stalking buckets
(research doc: docs/10-rate-limiting-and-behavioral-detection.md §1) —
bulk-inviting fresh accounts is a classic coordinated-abuse shape; keep
volumes human-scale.

Live status: schema-decoded 2026-09; live firing is the operator's call.

## fbk groups request-join

Request to participate in a **gated** group
(`GroupsCometRequestToParticipateMutation`) — the gated-group answer to
`join`. The difference: `join` is the forum-style direct join; groups with
membership questions answer the join path with the request flow, and this
command files the participation request directly. Membership-question
answers ride a separate server-side save in the web client; the CLI
submits the request without an answers payload, mirroring the decoded
wrapper.

```
fbk groups request-join --group-id ID
```

| Flag | Default | Semantics |
|---|---|---|
| `--group-id ID` | required | Numeric group id; rides `input.group_id` |

```
$ fbk groups request-join --group-id 12345678901234567
participation requested for group 12345678901234567: ok
```

Live status: schema-decoded 2026-09; live firing is the operator's call.

## fbk groups feed

Read a group's feed via `CometGroupDiscussionRootSuccessQuery` — the
group-root page's SSR preload replayed with the group id as the top-level
`groupID` variable.

```
fbk groups feed --group-id ID [--cursor CURSOR] [--limit N]
```

| Flag | Default | Semantics |
|---|---|---|
| `--group-id ID` | required | Numeric group id |
| `--cursor CURSOR` | none | Opaque `end_cursor` from the previous page — chain it to walk deeper |
| `--limit N` | 20 | Max stories to return (local slice; the wire shape is never edited) |

```
$ fbk groups feed --group-id 12345678901234567
Example User | call for papers closing friday | 98765432109876543 | https://www.facebook.com/groups/... 
-- 1 stories | has_next_page=True | end_cursor= AbCdEf...
{"stories": [...], "end_cursor": "...", "has_next_page": true}
```

Output notes: rendering goes through the shared story layer
(`src/commands/render.py`) — one line per story (actor, 60-char text
head, feedback id, permalink), then the page summary. The feedback id is
the react/comment target handle. Walked stories pass a stub/duplicate
filter (live symptom: attachment sub-nodes echo an empty duplicate Story
entry; research doc: docs/15-live-calibration-findings.md §P5-5). An
empty group feed is valid state, not a failure.

Live status: live-probed 2026-09 (group-root preload harvested and
replayed verbatim; the probe group id appears only as `groupID`).

## fbk groups post

Publish a post to a group through the shared composer mutation
(`ComposerStoryCreateMutation` via `FeedService.publish`). **Group posts
must carry the group context** — and they do, structurally: the publish
call rides the live-verified group composer locations
(`feedLocation: "GROUP"`, `renderLocation: "group"`) plus the group id
itself (research doc: docs/15-live-calibration-findings.md §P3 composer
ground truth). A plain feed publish without that context is a different,
wrong mutation call. **Mutation** — 40/day budget.

```
fbk groups post --group-id ID --text TEXT [--privacy {public,friends,private}]
```

| Flag | Default | Semantics |
|---|---|---|
| `--group-id ID` | required | Numeric group id; rides the composer's `groupID` |
| `--text TEXT` | required | Post text |
| `--privacy` | `public` | Audience `base_state` — the CLI name maps to the wire enum (`public`→`EVERYONE`, `friends`→`FRIENDS`, `private`→`SELF`) |

```
$ fbk groups post --group-id 12345678901234567 --text "first post"
posted to group 12345678901234567: 98765432109876543
```

Output notes: in-band echo under the id-ish trim; the human line prints
the first id-shaped value (the new post's id when the server echoes it).

Live status: the composer mutation and the group composer locations are
live-verified (docs/15 §P3); the group-context publish replays them —
live firing is the operator's call.

## fbk groups members

Read a group's member heads via `GroupsCometMembersRootQuery` — the
members page's SSR preload replayed with the group id as `groupID`.

```
fbk groups members --group-id ID [--limit N]
```

| Flag | Default | Semantics |
|---|---|---|
| `--group-id ID` | required | Numeric group id |
| `--limit N` | 20 | Max member rows to return (local slice) |

```
$ fbk groups members --group-id 12345678901234567
members: 2
Example Admin 11111111111111111 [ADMIN]
Example Viewer 12345678901234 (you)
```

Output notes: rows are a **bounded head, not the full roster** — the
admin edges (`group_admin_profiles`, role `[ADMIN]`) first, then the
newest members (`new_members`); the viewer's own row is marked `(you)`.
Rows deduplicate on id; the emitted `count` is the number of rows
returned, not the group's total membership (the server does expose the
full roster count on `group_member_profiles.count` — research doc:
docs/15-live-calibration-findings.md §P8-2 — but the head walk does not
emit it).

Live status: live-probed 2026-09 (probe group replayed; admin +
new-member heads walked, 288,213-member roster headed).

---

## pages

The pages family (research doc: docs/02-endpoint-surface-map.md §2.5
pages namespace, §2.10 engagement family). Timeline reads follow the
same verbatim-preload law: GET the page URL, harvest the SSR
registration for `ProfileCometTimelineListViewRootQuery`, replay it with
the page id substituted as the top-level `userID` variable. (The sibling
`ProfileCometTimelineFeedQuery` returns partial field exceptions on
replay; the list-view root is the clean-replaying content query.)
Mutations ship schema-decoded 2026-09 templates from the /pages/creation/
chunk set.

## fbk pages create

Create a new Page via the current additional-profile flow
(`AdditionalProfilePlusCreationMutation`).

```
fbk pages create --name NAME [--category CATEGORY]
```

| Flag | Default | Semantics |
|---|---|---|
| `--name NAME` | required | Page name; rides `input.name` |
| `--category CATEGORY` | server default | A single page category key (e.g. `'Public Figure'`); seeds the decoded input's `categories` list. Absent → the server defaults the category |

```
$ fbk pages create --name "Example Page" --category "Public Figure"
page created: 12345678901234567
```

Live status: schema-decoded 2026-09; live firing is the operator's call
(research doc: docs/11-opsec-and-session-engineering.md §2).

## fbk pages like

Like a Page by numeric id (`CometPageLikeCommitMutation`, source
`PAGE_TIMELINE`). **Mutation** — reaction-class write budget, 40/day.

```
fbk pages like --page-id ID
```

| Flag | Default | Semantics |
|---|---|---|
| `--page-id ID` | required | Numeric page id; rides `input.page_id` |

```
$ fbk pages like --page-id 12345678901234567
page 12345678901234567 liked: ok
```

Output notes: in-band echo; the `--json` payload keeps the response's
id-ish fields (the like state includes `page.is_viewer_fan` and
`subscribe_status` when the server echoes them).

Live status: schema-decoded 2026-09; live firing is the operator's call.

## fbk pages follow

Follow a Page by numeric id (`CometPageFollowCommitMutation` → the
decoded `actor_subscribe` input; the page id rides `input.subscribee_id`,
subscribe location `PAGE_PROFILE_HEADER`). **Mutation** — 40/day budget.

```
fbk pages follow --page-id ID
```

| Flag | Default | Semantics |
|---|---|---|
| `--page-id ID` | required | Numeric page id; rides `input.subscribee_id` |

```
$ fbk pages follow --page-id 12345678901234567
page 12345678901234567 followed: ok
```

Live status: schema-decoded 2026-09; live firing is the operator's call.

## fbk pages feed

Read a Page's timeline feed via `ProfileCometTimelineListViewRootQuery`.

```
fbk pages feed --page-id ID_OR_SLUG [--limit N]
```

| Flag | Default | Semantics |
|---|---|---|
| `--page-id ID_OR_SLUG` | required | Numeric page id **or vanity slug** — the service resolves both to the same timeline query |
| `--limit N` | 20 | Max stories to return (local slice) |

```
$ fbk pages feed --page-id examplepage
Example Page | new release notes are up | 98765432109876543 | https://www.facebook.com/...
-- 1 stories | has_next_page=False | end_cursor= 
{"stories": [...], "end_cursor": null, "has_next_page": false}
```

Output notes: same shared renderer as `groups feed` — actor, text head,
feedback id, permalink, then the page summary. Page timelines carry
stories as `HighlightPostUnit` nodes rather than bare `Story` nodes; both
spellings are normalized into the same rows and pass the same
stub/duplicate filter.

One vanity caveat, enforced loudly: a **vanity slug can only be resolved
through the page's own preloads** (the service GETs the page and harvests
its SSR registration). If that fetch yields no preload, the command
refuses with a clear error — pass the **numeric page id** instead
(unknown-exception exit 2).

Live status: live-probed 2026-09 (the list-view root replayed verbatim
from a live page's preloads).

---

## settings

The account-settings family (research doc:
docs/02-endpoint-surface-map.md §2.12). This is the most sensitive
surface in the tool: password reauth and reset-link mutations carry
stronger server-side challenges than content surfaces, and grazing the
settings surface incidentally is itself a risk signal — touch it only
when the operation demands it (research doc:
docs/11-opsec-and-session-engineering.md §4). One honest scope note from
the calibration: a direct password **change** is not on the www GraphQL
surface (it lives in Meta Accounts Center) — the web-legal writes fbk
exposes are the reauthentication verify and the reset-link email.

`show` is a read; `set-default-privacy`, `verify-password`, and
`send-reset-link` are **mutations** — 40/day budget.

## fbk settings show

Replay the settings queries and print trimmed payloads. Reads replay
`CometSettingsRootUSFNewCometQuery` (preloads on /settings/) and
`SettingsCategoryLayoutUSFNewCometQuery` (preloads on
/settings/privacy/ — harvested from the privacy tab when /settings/
itself does not carry it) with the pages' verbatim variables.

```
fbk settings show
```

```
$ fbk settings show
root keys      : ['viewer', ...]
categories keys: ['settings_categories', ...]
{"settings": {"root": {...}, "categories": {...}}}
```

Output notes: human mode prints only the top-level **key sets** of both
payloads. The `--json` payload keeps the top two levels of each — deeper
dicts collapse to key lists and lists to length fingerprints — so huge
settings documents stay emit-safe while template inspection stays
possible.

Live status: live-verified 2026-09 (both queries replayed verbatim).

## fbk settings set-default-privacy

Save the default post-privacy audience
(`CometPrivacySelectorSavePrivacyMutation`) — the write the composer's
privacy selector fires. The live-captured composer template replays
verbatim with exactly three substitutions: the `base_state` enum, the
viewer's actor id, and a fresh `client_mutation_id`; the captured
renderer scope and every other field re-ride untouched. **Mutation** —
40/day budget.

```
fbk settings set-default-privacy --privacy {public,friends,private}
```

| Flag | Default | Semantics |
|---|---|---|
| `--privacy` | required | Audience for new posts; the CLI name maps to the live-captured string enums (`public`→`EVERYONE`, `friends`→`FRIENDS`, `private`→`SELF`) |

```
$ fbk settings set-default-privacy --privacy friends
default privacy -> FRIENDS
```

Output notes: in-band echo; the next composer publish picks up the new
`base_state` server-side (no client-side state involved).

Live status: live-verified 2026-09 — the captured template is the
verified variable shape (research doc:
docs/15-live-calibration-findings.md §P3).

## fbk settings verify-password

Verify the current password via the reauthentication mutation
(`CometPasswordReauthenticationMutation`). **Why it exists:** this is
the write the web surface uses to prove session possession of the account
password — the gate that unlocks the sensitive settings mutations
server-side. fbk exposes it as the honest way to test the reauth plane
(and your jar's password agreement) without changing anything.

```
fbk settings verify-password [--password PASSWORD]
```

| Flag | Default | Semantics |
|---|---|---|
| `--password PASSWORD` | prompted | Current password; prompted via getpass (never echoed) when absent. `--password` exists for scripting — prefer the prompt |

```
$ fbk settings verify-password
Current password:
password verified: True
```

Password-handling discipline, enforced structurally: the password never
prints (getpass), never hits the journal (the journal records field
names only), and is scrubbed from the emitted payload even if the server
echoes it back — any string equal to the secret is replaced with
`<redacted>` before emission.

Exit contract: 0 when `reauth_is_successful` is true; **1 otherwise** —
a failed verification is a failed precondition, not an exception. Treat
error-class responses as a negative verdict too. **Mutation** — 40/day
budget; a failed attempt burns budget, so do not loop retries.

Live status: live-verified 2026-09 (sensitive mutation — reauth
challenges are stronger than content-surface writes).

## fbk settings send-reset-link

Email a password-reset link to the viewer
(`useFXSettingsSendPasswordResetLinkForViewerMutation`). The viewer's
email is bound server-side to the session; the mutation's only argument
is a fresh `client_mutation_id`.

**This is the most sensitive mutation in the tool.** It is
destructive-adjacent: the reset email lands in the account's inbox
immediately, it consumes the 40/day mutation budget, and it affects the
account's own email — a reset link sitting in an inbox is a live
credential artifact. Never wire it into automation loops; fire it only
deliberately, by hand.

```
fbk settings send-reset-link
```

```
$ fbk settings send-reset-link
reset link sent: True
{"mutation": "useFXSettingsSendPasswordResetLinkForViewerMutation", "sent": true}
```

Exit contract: 0 when the response carries `success`; **1 otherwise** —
an unconfirmed send is a failed precondition, never counted as sent.

Live status: live-verified 2026-09 (sensitive mutation — treat the
account's inbox as affected the moment it fires).

---

## Files that feed this guide

- `src/commands/messenger.py` — the four handlers, the 24-char head rule,
  send exit contract, listen frame lines
- `src/commands/friends.py` — nine handlers, the one-level mutation trim
- `src/commands/groups.py` — seven handlers, group composer locations,
  id-ish response trim
- `src/commands/pages.py` — four handlers, vanity-slug resolution
- `src/commands/settings.py` — four handlers, getpass + `_scrub_secret`
- `src/surfaces/messenger.py` — thread/history/send contracts, the
  Lightspeed fallback, listen orchestration, EB range semantics
- `src/surfaces/friends.py` — root query, status enums, the six mutation
  templates and their id fields
- `src/surfaces/groups.py` — create/join/add-members/request-join
  templates, feed/members preload replays, stub/duplicate filter
- `src/surfaces/pages.py` — create/like/follow templates, list-view
  timeline replay, HighlightPostUnit parsing
- `src/surfaces/settings.py` — settings queries, the captured
  privacy-save template, password/reset mutations
- `src/realtime/ws.py` — the fingerprint-coherent WS factory
  (curl_cffi primary, `websockets` fallback)
- `src/realtime/mqtt/client.py` — MQIsdp lifecycle, keepalive cadence,
  CONNACK/SUBACK enforcement
- `src/realtime/mqtt/connect.py` — the CONNECT identity blob
- `src/realtime/mqtt/frames.py` — MQIsdp packet codec (the PUBLISH
  one-byte quirk)
- `src/realtime/mqtt/thrift.py` — the IDL-free Thrift-compact reader
- `src/realtime/dgw/client.py` — DGW channel lifecycle, `{"code":200}`
  handshake, x-dgw-* URL parameters
- `src/realtime/dgw/frames.py` — the 6-byte DGW frame codec
- `src/realtime/dgw/requests.py` — the lightspeed request catalog and
  request-id correlation
- `src/commands/common.py` — global flags, `emit` stdout discipline,
  `run_command` exit-code contract
- `src/commands/render.py` — the shared story renderer used by
  `groups feed` and `pages feed`
- `src/domain/common.py` — `ThreadSummary`, `Message`, `User`, `Privacy`
- `src/constants.py` — broker URLs, topics, keepalive, app ids
