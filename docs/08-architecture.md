# 08 — Architecture

Internal design of `fbk` (v2.2.0) for contributors. The operator-facing
guides live alongside this one in `cli/docs/`; the repo-root research suite
(`docs/01`–`docs/16` at the repository root) is the ground truth for wire
shapes and is cited here by plain filename. Read this page before touching
anything under `src/`.

The one-sentence summary: **every request funnels through one governed,
fingerprint-coherent transport, surfaces never touch HTTP, and the CLI
layer only translates argparse into surface calls and typed errors into
exit codes.**

## The layer map

Dependencies point strictly downward; nothing imports `commands/`.

| Layer | Modules | Responsibility |
|---|---|---|
| CLI | `app.py`, `commands/*` | argparse tree assembly (one module per family, `_COMMAND_MODULES` order = the help listing), process-level setup (UTF-8 stream reconfigure, `allow_abbrev=False` tree-wide), dispatch via `commands.common.run_command` (typed error → exit code), `emit()` stdout discipline |
| Session facade | `session.py` | The single composition point: cookie jar → profile → journal → governed transport → token cache. Nothing else constructs an `FBTransport`, `GraphQLClient`, or `TokenCache`. Lazy `registry` parse; cache-first `bootstrap()` |
| Surface | `surfaces/*`, `surfaces/base.py` | One service class per Facebook surface (feed, messenger, groups, ...). Wire knowledge only: friendly names, variable assembly, response decode into `domain/common.py` models. Never touches HTTP — everything delegates through `Session`/`GraphQLClient`, which keeps every service testable against stubs (`tests/fakes.py`) |
| GraphQL | `graphql/client.py`, `parsing.py`, `errors.py`, `registry.py`, `registry_refresh.py` | Persisted-query calls to `POST /api/graphql/` (no query text ever ships — `doc_id` resolves server-side), streamed-NDJSON parsing and deep merge, the typed error taxonomy, and doc_id resolution/harvest |
| Transport | `transport/session.py`, `headers.py`, `cookies.py`, `profile.py` | `FBTransport`: curl_cffi `chrome136` TLS/h2 impersonation (pinned, versioned — the unversioned `chrome` preset would silently rotate the fingerprint), canonical header overlays, Netscape jar ingestion, `ClientProfile` coherence validation. Governor gate runs before I/O on every governed method |
| Auth | `auth/bootstrap.py`, `state.py`, `logout.py` | One homepage GET → login-state classification (`LoginState`), CSRF pair (fb_dtsg + lsd) harvest, deploy revision, bundle census, SSR preload registry; the `logout.php` form POST pair |
| Realtime (parallel branch off the cookie jar) | `realtime/ws.py`, `realtime/mqtt/*`, `realtime/dgw/*` | Fingerprint-coherent WebSocket factory + the MQTT and DGW protocol clients (below) |
| Cross-cutting | `governor.py`, `journal/recorder.py`, `token_cache.py`, `stats.py`, `retry.py`, `domain/common.py`, `commands/render.py`, `constants.py`, `config.py` | Pacing/budgets, redacted JSONL journals, the persistent token cache, nearest-rank percentile, command-layer retry, typed domain models, shared story renderers, the single source of live-calibrated protocol constants, and path/config discovery |

## Request flow: `fbk feed read` end-to-end

One invocation, start to finish. Step numbers are stable; cite them in
reviews.

1. **argv → dispatch.** `app.main` reconfigures stdout/stderr to UTF-8
   (Windows consoles default to cp1252 and choke on Bangla/emoji payloads),
   builds the parser (each family module's `register(sub)` attaches its
   subparsers), disables prefix abbreviation tree-wide, parses, and calls
   `commands.common.run_command(args.fn, args)`.
2. **Config discovery.** `new_session(args)` → `build_config` →
   `Config.discover`: root resolves from the `--root` flag → `FBK_ROOT` →
   the package root derived from `src/config.py`' own location
   (`parents[1]` = `cli/`, the pyproject parent). `--cookies`/`FBK_COOKIES`
   override the jar path. Every derived path (`data/`, `state/`,
   `cookies.txt`, `profile.json`) lands inside that root.
3. **Session construction.** `Session.__init__` wires the stack in order:
   `load_netscape` parses the jar (facebook.com rows only; zero rows →
   `CookieLoadError` → exit 7), `load_or_default` loads/validates the
   client profile, the JSONL journal binds to `state/<command>.jsonl`
   (here `state/feed.jsonl`) unless `--no-journal`, then — lazily, never
   at module import — `_resolve_transport()` imports `transport.session`
   and constructs `FBTransport` with the process-wide governor
   (`governor.default_governor()`). `TokenCache` binds to
   `state/token_cache.json`.
4. **Bootstrap, cache-first.** `FeedService(session).read()` needs
   `session.graphql`, which first calls `session.bootstrap()`. The token
   cache is checked first: a fresh entry (15-minute TTL) builds the
   `Bootstrap` with **zero HTTP requests**. Only on cache miss does
   `bootstrap_homepage` run — one governed GET of
   `https://www.facebook.com/`, classified into a `LoginState`
   (marker sets, the authoritative `"is_checkpointed":true` flag, the
   `USER_ID == c_user` coherence check), harvesting fb_dtsg, lsd, viewer
   identity, deploy revision, bundle URLs, and the SSR preload registry.
   The result is cached to disk and journaled (`session_start` with
   `Bootstrap.safe_dict()` — token values fingerprinted, never cleartext).
5. **Registry, lazily.** First registry access parses the freshest
   `data/doc_id_registry_*.json` (priority: v3 → v2 → merged → full;
   per-file fail-soft descent; corrupt-but-present files skip with a
   warning). Commands that never run GraphQL never pay the ~180 KB parse.
6. **doc_id + variables resolution (the read path).** For feed page 1 the
   service prefers the **SSR preload entry** the bootstrap harvested — the
   homepage literally embeds `CometModernHomeFeedQuery` with the exact
   `queryID` and verbatim variables the server itself used during SSR;
   replaying them is the most faithful, bypass-resistant read available
   (docs/15-live-calibration-findings.md §P2-2). Fallbacks: the registry's
   doc_id for the query, and the baked `DEFAULT_FEED_VARIABLES` when no
   preload exists. Page 2+ (`paginate`) resolves
   `CometNewsFeedPaginationQuery` through the registry and echoes the
   opaque `end_cursor` verbatim.
7. **The GraphQL call.** `GraphQLClient.call(friendly, doc_id, variables)`
   → `call_raw` builds the canonical form body:
   `fb_api_req_friendly_name`, `fb_api_caller_class=RelayModern`,
   compact-JSON `variables`, `doc_id`, `server_timestamps=true`,
   `q=<transport counter>`, plus the **CSRF pair** — `fb_dtsg` (form field)
   and `lsd` (form field; also shipped as the `x-fb-lsd` header). Under
   `--dry-run` the plan is printed here (variables redacted) and
   `DryRunComplete` ends the run before anything is consumed.
8. **Governor gate, then the wire.** `FBTransport.post_graphql` runs
   `governor.before_request(is_mutation=...)` **before** any I/O (the
   `Mutation` friendly-name suffix routes onto the separate mutation
   budget), then POSTs via curl_cffi with the `graphql_headers` overlay
   (content-type, origin, referer, accept-language, `x-fb-friendly-name`,
   `x-fb-lsd`) over the chrome136 impersonated TLS/h2 identity.
9. **Journal + soft-block observation.** The transport appends a journal
   record — method, URL, status, content length, and body field *names*
   only — and checks the response for the soft-block signature
   (HTTP 200 with an empty body, 403, 429); any hit calls
   `governor.observe_soft_block()`, engaging the persistent cooldown.
10. **Status guard.** 301/302 to `/checkpoint` → `CheckpointError`; to
    `/login` → `NotLoggedInError`; 403/429 → `RateLimitedError`.
11. **Parse.** `strip_legacy_prefix` removes the `for (;;);`
    anti-JSON-hijacking guard when present; `parse_incremental` decodes
    the back-to-back NDJSON Relay frames (a torn trailing frame is left,
    never raised on); **zero parseable documents on a 200 is itself the
    soft-block signature** → `RateLimitedError`. `merge_docs` deep-merges
    the frames (later frames carry refreshed slices; last write wins).
12. **Error guard (first error in `errors[]` wins).** Empty/absent `errors`
    IS success. Otherwise the code classifies via
    `constants.GRAPHQL_ERROR_CODES`: NOT_LOGGED_IN → `NotLoggedInError`;
    the DTSG-rejection pair (1357051/1677047) → one silent re-bootstrap +
    retry (`_refresh_tokens` invalidates and re-saves the persistent
    token cache); DOC_ID_UNKNOWN → `DocIdStaleError`;
    CHECKPOINT_REQUIRED → `CheckpointError` *after*
    `governor.observe_checkpoint()`; RATE_LIMITED_SUSPECTED →
    `RateLimitedError` *after* `governor.observe_soft_block()`; anything
    unmapped (live-confirmed 1675012 variable coercion) →
    `GraphQLProtocolError` with the full envelope attached.
13. **Surface decode.** `parse_feed_page` walks the merged payload
    (pre-order DFS, document order), extracts every
    `__typename == "Story"` node into the typed `Story` domain model
    (story key, `FeedbackID` UFI handle, actor, text, permalink), and
    regex-extracts `page_info.end_cursor` / `has_next_page` for chaining.
14. **Emit + exit.** `emit()` runs the human renderer
    (`commands/render.print_stories`) then prints the compact one-line
    JSON (or the indented payload alone under `--json`); errors went to
    stderr already; `run_command`'s typed-error barrier maps any raised
    error to its exit code. `with_session` closes the transport on scope
    exit.

## The doc_id reality

Persisted-query ids are deploy-allocated and **rotate with Facebook's
build pipeline**. Nothing in the shipped JS pins them permanently; ids
harvested together usually die together on a single build push. Live
evidence: `CometUFIDeleteCommentMutation`'s old registration died with
deploy revision 1047963790 — the stale id returned the doc-id-mismatch
error class in a live replay, and a fresh full harvest (written to v3)
restored it. This is the steady-state drift cycle, not an exception.

Three resolution sources, in the order the code consults them:

| Source | What it holds | Who wins |
|---|---|---|
| SSR preload entries (bootstrap harvest) | The `queryID` + verbatim variables the server itself used for a page's queries | Feed-style reads, when the bootstrap carried the entry (`FeedService.read`) |
| `constants.KNOWN_MUTATIONS` | The live-verified mutation catalog: friendly name → doc_id, source-tagged (first block replayed end-to-end; second block bundle-harvested/schema-derived) | Every write path — `Surface._mutation_doc_id` prefers this table over the registry because each entry was fired and verified against the live edge |
| Harvested registry (`data/doc_id_registry_v3.json` → v2 → merged → full) | ~1,031 friendly_name → doc_id pairs from rsrc.php bundles | Reads without a preload entry, and mutations absent from KNOWN_MUTATIONS. A miss in all of them raises `RegistryMissError` (exit 6) |

The refresh pipeline (`graphql/registry_refresh.py`, `fbk registry
refresh`) closes the loop: bootstrap the logged-in homepage, collect the
current deploy's rsrc.php bundle URLs (~680 on a live load), download them
on a thread pool (one thread-local curl session per worker, polite jitter,
every fetch gated through the same governor), and extract pairs with the
two live-calibrated shapes — the `__d("<Name>_facebookRelayOperation"...)`
module export and the Relay `params:{id:...}` registration. With
`--save` the result is **merged** into `data/doc_id_registry_v3.json`
(fresh ids win; names the homepage deploy no longer references are
*carried* from the baseline, because a homepage-only harvest would
otherwise lose lazy action-surface bundles). v2 is never overwritten.
`fbk registry diff` (offline v2-vs-v3) is the pre-flight of the
maintenance loop; `fbk templates verify` audits the captured assets the
same way. Research depth: docs/13-recon-methodology.md §2,
docs/04-graphql-protocol-deep-dive.md §4, §10.

## Error taxonomy and exit codes

Every structured failure is typed; `commands.common.run_command` maps the
type to a stable exit code (a public interface — scripts and CI gates
match on these; renumbering is a breaking change). The authoritative
contract table lives in `run_command`'s docstring; `cli/docs/11-troubleshooting.md`
is the operator view.

| Exception (graphql/errors.py unless noted) | Raised by / wire trigger | rc | Operator response |
|---|---|---|---|
| `FBGraphError` (base) | Base of the structured family; carries `code` + raw envelope | 2 | Read the message; family-specific guidance below |
| `NotLoggedInError` | 1357004 hard logged-out; 1357051/1677047 DTSG rejection *after* the client's one auto-refresh failed; 302 → `/login`; bootstrap with no fb_dtsg | 3 | Re-export the cookie jar from a logged-in browser |
| `CheckpointError` | 1384000-family envelope; 302 → `/checkpoint` | 4 | Halt and contain — the governor has already engaged its 6-hour disengagement; do not retry the jar |
| `RateLimitedError` | HTTP 403/429; 1357046 envelope; 200-but-unparseable body (empty-200 soft block) | 5 | Disengage into the governor cooldown; only `--retry N` (jittered backoff, this error class only) is sanctioned |
| `DocIdStaleError` | 1570245-family "Query with id ... not found" — the deploy rolled the doc_id | 2 | Run `fbk registry refresh --save`, not ad-hoc retries |
| `GraphQLProtocolError` | Any unmapped structured error (live-confirmed: 1675012 variable coercion) | 2 | Fix the caller's variables shape; the same body fails identically on retry |
| `RegistryMissError` | Strict `doc_id` lookup miss, or no registry file at all | 6 | Re-harvest (`fbk registry refresh`) |
| `RegistryLoadError` (`RegistryMissError` subclass) | Registry file present but corrupt | 6 | Delete or re-harvest the named file |
| `DryRunComplete` (success sentinel, NOT an FBGraphError) | `--dry-run` printed the request plan; nothing was sent or debited | 0 | None — this is success |
| `DryRunRawSeamError` (outside the family) | A raw-seam command (upload, video upload) under `--dry-run` — the raw HTTP seam has no request plan | 1 | Re-run without `--dry-run` if the action is intended |
| `CookieLoadError` (transport/cookies.py) | Jar missing, unparseable, or zero facebook.com rows | 7 | Fix/replace cookies.txt |
| `FingerprintRejectedError` (transport/session.py) | No supported curl_cffi impersonation target, or an incoherent client profile | 8 | Fix the profile / curl_cffi install; retrying the session cannot succeed |
| `GovernorBlockedError` (governor.py) | Cooldown active, hourly/daily/mutation cap hit, or outside the active window — raised before any socket opens | 2 | **STOP, not retry.** Back off; resume inside the window/budget |
| `BootstrapPageError` (auth/bootstrap.py) | Homepage GET returned 200 with an empty body — the edge soft-block signature | 2 | Disengage per containment; never retry-escalate |
| `KeyboardInterrupt` | Operator interrupt, any stage | 130 | — |

Exit code 1 is reserved for failed *preconditions* the handler itself
reports (unconfirmed mutations, `--show` contradictions, missing journals)
— no exception involved. Any other unexpected exception escaping `main()`
also exits 2.

## The realtime stack

`fbk messenger listen` (and `--dgw`) ride this stack. Protocol ground truth
is research doc docs/06-messenger-mqtt-protocol.md; every dialect fact
below is live-confirmed 2026-09 (docs/15-live-calibration-findings.md).

### One fingerprint for every socket (the split-identity lesson)

Until Phase 10 the HTTP transport spoke chrome136 TLS/h2 (curl_cffi) while
the MQTT and DGW sockets spoke the `websockets` library's default
Python-ssl ClientHello — the same account presenting two different TLS
identities on the same edge is an observable split-identity signal.
`realtime/ws.py` fixes this: **every** realtime connection — MQTT and all
four DGW channels — dials through `coherent_ws_connect`, curl_cffi's WS
client inheriting the session's impersonation and cookie jar. The legacy
`websockets` path survives only as a fallback for curl_cffi builds with
no sync WS API, and a genuine handshake failure never falls back
(retrying the same endpoint with a different ClientHello is precisely the
signal Phase 10 eliminated). A daemon reader thread bridges curl's
blocking `recv()` into deadline-aware pops.

### The MQTT dialect (legacy Messenger bus)

`realtime/mqtt/client.py` + `frames.py` + `connect.py` + `thrift.py`:

* **This is not standard MQTT 3.1.1.** Protocol name `"MQIsdp"`, level 3
  (not `"MQTT"`/4); a vanilla 3.1.1 CONNECT is parsed and gracefully
  refused with a clean WS 1000 close.
* **The CONNECT username is a plain-UTF-8 JSON identity blob** (UA, app
  id, device uuid, session int, uid), connect flags 0x82 (username +
  clean session); the client id prefix is `mqttwsclient`. fbk can replay
  the captured 452-byte CONNECT verbatim or build a fresh identity in
  the captured structure — both yield CONNACK rc=0 (the docs/15 §P3-1
  proof). The UA passes through `sanitize_user_agent` (the live capture
  rode `HeadlessChrome/152` — that must never ship).
* **Broker:** `wss://edge-chat.facebook.com/chat`
  (`?region=cln&sid=<id>&cid=<uuid>`); `edge.facebook.com` is NXDOMAIN
  dead. Cookies ship in the WS handshake; `c_user`/`xs` are
  hard-validated at the upgrade.
* **Handshake:** server-first `0x0A` byte (consumed, non-fatal if absent)
  → CONNECT → CONNACK rc must be 0 → SUBSCRIBE `/t_ms` + `/t_rtc_multi`
  (SUBACK required; 0x80 = rejection = hard error).
* **Topics:** `/t_ms` (classic Mercury delta bus — dead for chat delivery
  since E2EE; retained for delta capture), `/t_rtc_multi` (the only topic
  the production client was observed subscribing live), `/t_rtc`,
  `/$pc` presence, `$typ` typing.
* **Keepalive:** 15 seconds negotiated in CONNECT; fbk pings at a 10s
  cadence — MQTT requires the *client* to emit a control packet inside
  the window even while inbound traffic streams.
* **Deltas:** PUBLISH bodies are Thrift-compact; `CompactReader`
  decodes IDL-free (field headers, zigzag varints, length-prefixed
  strings, list headers), keying results by field id — schema drift
  stays observable. PUBLISH framing has one extra unexplained byte
  between the remaining-length varint and the topic string; skipping it
  is mandatory or every topic/payload split mis-parses.

### The DGW alternative (data-gateway GraphQL-over-WS)

`realtime/dgw/client.py` + `frames.py` + `requests.py`:

* **Channels:** `wss://gateway.facebook.com/ws/<channel>` with
  `rpsignaling`, `realtime`, `lightspeed`, `streamcontroller`; query
  params are the `x-dgw-*` set (appid = the Messenger web app id,
  version 5, tier prod, authtype `1:0`, `x-dgw-uuid` = c_user,
  per-socket deviceid, loggingid on rpsignaling/lightspeed). Cookies are
  the only credential material; all four channels replay clean.
* **Frame model:** `opcode | seq(2B LE) | len(2B LE) | flags(1B) |
  payload`. Opcodes: `0x0F` control (JSON; the handshake body is `{}` and
  the ack must be `{"code":200}`), `0x0D` data (task envelopes with the
  `\x00\x80` binary prefix), `0x0C` ACK8 (echoes seq, 2-byte `\x00\x00`
  payload), `0x0E` ACK3 (3 bare bytes — op + seq, no header; the frame
  *splitter* owns this exception), `0x09`/`0x0A` ping/pong keepalives.
  One WS message may concatenate several DGW packets; `split_ws_frame`
  walks the offsets.
* **Lightspeed dialect** (`LightspeedClient`): request envelopes
  `{"app_id", "payload", "request_id", "type"}` where type 3 =
  client_subscribe (thread-list task + per-thread tasks), 1 =
  state_sync, 2 = state_sync_delta; responses echo `request_id` (null =
  server-initiated push). `messenger listen --dgw` opens the lightspeed
  socket, completes the `{"code":200}` handshake, and sends the captured
  client_subscribe shape.

Message sync for chats now rides the DGW lightspeed channel, not
`/t_ms` — the MQTT client is retained for protocol completeness and
delta capture. Read docs/06-messenger-mqtt-protocol.md before touching
anything in `realtime/`.

## The journal recorder

`journal/recorder.py` + `transport/session.py._note`: every governed (and
raw-seam) request appends one JSONL record to the command's journal under
`state/`:

```json
{"ts": <epoch>, "method": "POST", "url": "https://www.facebook.com/api/graphql/",
 "status": 200, "content_length": 941824, "ctx": {"body_fields": ["fb_api_req_friendly_name", "doc_id", "fb_dtsg", "lsd", "q", "variables"]}}
```

plus event records (`session_start` with the fingerprinted bootstrap
view). The invariant is **REDACTION AT WRITE**: `JSONLJournal.record`
passes every entry through `redact_entry` — the `ALL_SECRETS` vocabulary
(cookie names `xs, datr, c_user, sb, fr, presence`; token names
`fb_dtsg, lsd, access_token, sessionid, privacy_write_id`; plus
`logout_hash` and `async_get_token`) — so any value under a secret-named
key is replaced by `<redacted:<12-hex sha256 fingerprint>>` *before* the
line is written. No caller can bypass it (the single-argument API makes
the whole mapping the unit of redaction), no journal line reaches disk
unredacted, and secrets live only in `cookies.txt` and process memory.
Body fields are journaled by NAME, never value. Append-per-record with no
fsync by design (the journal sits on the latency path of every paced
request; a crash can tear only the trailing line, which costs one record
of forensic continuity, never a secret). The journals are the study's
dataset: versioned, greppable, safe to keep.

## Design invariants

Properties the codebase enforces structurally. If a change violates one
of these, the change is wrong.

1. **Every request passes the transport + governor — no call-site bypass.**
   `get`/`post`/`post_graphql` all run `_govern()` before I/O; surfaces
   hold zero pacing logic and cannot burst by accident. The one deliberate
   exception is the rupload raw seam (`raw_post`/`raw_get`) for
   hand-built multipart media transfers — still soft-block-observed and
   centrally journaled with a surface tag, and the GraphQL publish that
   concludes each upload carries the mutation budget. The GraphQL
   client's error guard additionally *escalates into* the governor on
   checkpoint/rate-limit envelopes, so containment engages before the
   exception even propagates.
2. **`cli/` self-containment.** The `cli/` directory is the package
   root; every runtime path resolves inside it (`Config.discover`
   anchors on the module's own location, never on cwd), so the tree can
   be copied, zipped, and vendored. Operator-supplied *names* (draft
   names, journal names) are validated at the boundary — empty strings,
   dot-segments, and anything carrying a path separator are rejected
   before a path is ever resolved, so no invocation can read or write
   outside `state/drafts/` or `state/`.
3. **Lazy curl_cffi import.** curl_cffi costs ~200 ms to import and the
   2026-09 startup audit found ~93% of per-invocation CLI cost is import
   overhead — so it is imported only when a transport or socket is
   actually constructed (`FBTransport.__init__`, `resolve_impersonate`,
   `ws_connect`), never at module import, with `TYPE_CHECKING`-only
   annotations. Offline commands (config, journal, doctor, templates,
   governor status/audit, registry lookups) never pay for it.
4. **`data/` immutable, `state/` mutable.** `data/` holds captured wire
   payloads and registries and is never written at runtime; `state/`
   holds journals, `governor_state.json`, `token_cache.json`, and
   `drafts/`. State writes are atomic (temp file + `os.replace`) and
   fail-soft — a corrupt state file resets to defaults rather than
   taking commands down.
5. **Offline-first testability.** Every surface is testable against
   stub sessions (`tests/fakes.py`) and the *real* captured fixtures in
   `data/` (930+ offline tests); live integration tests are
   `FBK_LIVE=1`-gated. The `--dry-run` flag is the same discipline at
   runtime: full request plan, zero wire/governor/journal/q consumption.
6. **Emit discipline.** Payload data on stdout only; error text on stderr
   only. Default mode runs the human renderer then prints one compact
   JSON line (scripts pipe the last line); `--json` prints the indented
   payload alone. Shared story renderers live in `commands/render.py`
   so every timeline-shaped surface prints identically.

## Files that feed this guide

* `src/app.py`, `src/commands/common.py`, `src/commands/render.py`
* `src/session.py`, `src/config.py`, `src/constants.py`
* `src/governor.py`, `src/retry.py`, `src/stats.py`, `src/token_cache.py`, `src/drafts.py`
* `src/domain/common.py`
* `src/surfaces/base.py`, `src/surfaces/feed.py`, `src/commands/feed.py`
* `src/graphql/client.py`, `src/graphql/parsing.py`, `src/graphql/errors.py`,
  `src/graphql/registry.py`, `src/graphql/registry_refresh.py`
* `src/auth/bootstrap.py`, `src/auth/state.py`, `src/auth/logout.py`
* `src/transport/session.py`, `src/transport/headers.py`,
  `src/transport/profile.py`, `src/transport/cookies.py`
* `src/journal/recorder.py`, `src/journal/analysis.py`
* `src/realtime/ws.py`, `src/realtime/mqtt/{client,connect,frames,thrift}.py`,
  `src/realtime/dgw/{client,frames,requests}.py`
* `src/commands/messenger.py`, `src/surfaces/messenger.py`
* `cli/README.md`, `cli/docs/README.md`
* Research suite (repo root, cited by filename):
  docs/04-graphql-protocol-deep-dive.md,
  docs/06-messenger-mqtt-protocol.md,
  docs/12-python-tooling-architecture.md,
  docs/13-recon-methodology.md,
  docs/15-live-calibration-findings.md,
  docs/16-client-realism-and-detection-landscape.md

Related guides: [09-safety-and-opsec.md](09-safety-and-opsec.md) (the
governor policy in full), [11-troubleshooting.md](11-troubleshooting.md)
(the operator's exit-code view).
