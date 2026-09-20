# 03 — Session, auth, and the global flags

This guide covers the session model — what one `fbk` invocation does from
cookie jar to armed GraphQL client — the login-state machine, the persistent
token cache, the commands `fbk login`, `fbk whoami`, `fbk state`, `fbk logout`,
and `fbk cookies inspect`, and the global flags every command shares. Path
resolution, env vars, and the transport profile are covered in
[02-configuration.md](02-configuration.md). Wire-format grounding lives in
the research suite — e.g. research doc:
docs/03-authentication-and-session-model.md (session model, cookies),
research doc: docs/15-live-calibration-findings.md (§2 token shapes,
calibrated 2026-09).

## 1. One invocation, end to end

Every live command travels the same pipeline (src/session.py — the `Session`
facade is the single dependency every surface takes):

1. **Config resolve** — `Config.discover()` per
   [02-configuration.md](02-configuration.md) §1 (`--root` / `FBK_ROOT`,
   `--cookies` / `FBK_COOKIES`).
2. **Cookie jar load** — `cookies.txt` is parsed in Netscape format with the
   stdlib `MozillaCookieJar` (including the `#HttpOnly_` variant); only rows
   scoped to `facebook.com` are kept, flattened to `{name: value}` and
   injected into the curl_cffi session jar. Expiry and attribute enforcement
   are delegated to the session jar, which replicates the browser's policy.
   The jar must carry the auth pair **`c_user` + `xs`** — the server validates
   the pair as a unit (`xs` is the bearer credential, `c_user` the uid claim
   it must agree with). A torn jar — non-empty but rejected by the strict
   parse (one mangled line in a hand-edited or truncated export) — now
   **self-heals**: a tolerant line-level reparse (src/transport/cookies.py,
   `_tolerant_facebook_rows`) drops the genuinely mangled non-comment lines
   and keeps the surviving seven-field rows, healing only when `facebook.com`
   rows still yield; Session records a `cookie-jar-heal` event in
   `state/healing.jsonl` with the dropped-row count. A jar the reparse cannot
   rescue — missing, every row mangled, or zero `facebook.com` rows — raises
   `CookieLoadError` → exit 7; an empty jar must never silently produce an
   anonymous session.
3. **Transport construction** — curl_cffi impersonating `chrome136`
   (fallback chain `chrome136 → chrome131 → chrome124 → chrome120`), with
   the client profile's coherence validated at construction. An incoherent
   profile means `FingerprintRejectedError` → exit 8.
4. **Page bootstrap** (cache-first — see §3): a single GET of
   `https://www.facebook.com/` through the governed transport, then:
   * classify the served page into a `LoginState` (§2),
   * harvest tokens: `fb_dtsg` (the mutating-call CSRF token, live format
     `NAf…:1:<unix-expiry>` — the older `AQ` prefix is stale pre-2024
     format) and `lsd` (the anti-CSRF token; three ship per page, the first
     is the one the browser itself sends), plus viewer identity, deploy
     revision, and the SSR preload registry.
   * on a cache hit, the entry must agree with the jar: an entry whose
     `user_id` differs from the jar's `c_user` is discarded *before* use —
     a cache bound to a swapped jar would otherwise burn one doomed request
     before the DTSG auto-refresh cured it (§3).
   * on a cache miss, a degenerate page — classified `unknown`, or
     `logged_in` while carrying no `fb_dtsg` — earns exactly **one**
     governed retry (each bootstrap GET is governor-paced); the healthy
     retry page is adopted, and persistent degeneracy propagates honestly.
     A `checkpoint` classification is never retried — enforcement is met
     with disengagement, not retry-escalation.
5. **GraphQL client armed** — the persisted-query client is built over the
   bootstrap with the registry, endpoint, and CSRF pair wired in.

The only hard bootstrap failure is HTTP 200 with an empty body — the edge
soft-block signature (`BootstrapPageError`); the correct response is
disengagement, never retry-escalation.

## 2. The login-state machine

Classification is **evidence-driven per served page** — no code path may
assume state from a prior response (research doc:
docs/03-authentication-and-session-model.md §3).

| State        | Meaning                                                        | Evidence |
|--------------|----------------------------------------------------------------|----------|
| `LOGGED_IN`  | `c_user`/`xs` accepted, DTSG harvested                         | auth cookies present + login markers (`CurrentUserInitialData`, `DTSGInitData`) with page uid == `c_user`; or `cookies_only` |
| `LOGGED_OUT` | login form served, no auth cookies, or uid mismatch              | login-form markers without login markers; `no_auth_cookies`; or `uid_mismatch` (the page's `USER_ID` ≠ `c_user` — the `xs` binding no longer points at this viewer) |
| `CHECKPOINT` | integrity challenge                                             | **`"is_checkpointed":true` in the config frames** |
| `UNKNOWN`    | page carried none of the above evidence                         | no markers |

A fifth enum value, `SHADOW_SUSPECT`, is reserved in `auth/state.py` for the
200-but-degraded heuristic (research doc:
docs/10-rate-limiting-and-behavioral-detection.md §3) but is not currently
produced by the classifier.

**The authoritative checkpoint signal is the `is_checkpointed` flag — NOT
`/checkpoint` URL string-matching.** The string `/checkpoint` appears in URL
routing tables on perfectly healthy pages and false-positives; it is trusted
only as the post-redirect `final_url`. Do not downgrade detection to URL
matching anywhere downstream.

Every bootstrap also scans for account-level restriction banners; an
`ACCOUNT_WARNING:…` marker means the account is flagged for automated
behaviour — stop all automated activity entirely (containment, not
retry-escalation).

## 3. The persistent token cache

`state/token_cache.json`, **15-minute TTL** (src/token_cache.py). Without it,
every invocation bootstrapped the full homepage (~2–5 MB, 1 request) just to
harvest tokens. With it, **one command = one request** while the cache is
fresh: a warm invocation satisfies the bootstrap from disk and spends its
single request on the actual API call.

Behavior details:

* A cache hit constructs the bootstrap with state `LOGGED_IN` and a
  `token_cache` marker — an on-disk entry only ever exists for a previously
  verified login, and the GraphQL client's auto-refresh catches revoked
  tokens anyway.
* Only entries harvested in the `logged_in` state round-trip — a checkpoint
  or logged-out bootstrap never caches as reusable tokens.
* The file holds the token pair (`fb_dtsg`, `lsd`) and non-secret metadata
  (user_id, revision) — **never cookie values**. Writes are atomic (temp +
  replace); a corrupt or hand-edited file fails soft to a cache miss — and
  the miss is *diagnosed*: `TokenCache.load_diagnosed` pairs it with a
  reason token (`ok` on a fresh hit; `absent` / `expired` / `stale-state` /
  `corrupt: <exception repr>` / `invalid-shape` on a miss), the hook the
  self-healing coordinator reads to tell the benign misses
  (`absent`/`expired`/`stale-state`) from the heal-worthy ones
  (`corrupt`/`invalid-shape`) without re-reading the file. Identity
  coherence is the one check `load_diagnosed` deliberately does **not**
  perform: it has no cookie jar to compare against, so the entry-vs-jar
  check lives in `Session` (below).
* Live DTSG tokens carry their own expiry in the `:1:<unix>` suffix (a
  multi-day validity window), so 15 minutes is far inside server-legal
  reuse.

**The corrupt-cache self-heal.** A cache classified `corrupt` or
`invalid-shape` is discarded — the discard IS the heal: the fail-soft
contract already turned the damaged file into a cache miss, and the full
bootstrap that follows regenerates it. Session records a
`token-cache-rebuild` event in `state/healing.jsonl` (capped at 2 per
invocation, src/healing.py) so the extra bootstrap request is auditable;
`fbk doctor`'s self-healing check surfaces the census. The benign misses
(`absent`, `expired`, `stale-state`) are ordinary cache behavior, never
heal events.

**The identity-mismatch self-heal.** A *fresh* entry can still be wrong in
one precise way: bound to a different account than the jar now carries —
you swapped `cookies.txt` while `state/token_cache.json` persisted. The
coherence check runs in `Session.bootstrap`: an entry whose `user_id`
differs from the jar's `c_user` is discarded **before use**, so the run
costs zero wasted requests — previously the stale binding burned one
doomed API call before the DTSG-rejection auto-refresh cured it. The
event rides the same `token-cache-rebuild` kind with the trigger
"token cache identity mismatch — discarded", and it is audit-only and
uncapped (`HealingContext.log_event`): like the jar and bootstrap heals,
it records a repair that happened outside the invocation-capped call
loop.

**The session-level registry heal.** Corruption and staleness now heal at
the session layer too: when **every** registry tier on disk is corrupt
(`RegistryLoadError` — the one failure the per-file fail-soft descent
cannot route around), `Session.registry` runs the coordinator's capped,
cooled-down re-harvest, verifies the result, and adopts the fresh
registry via the `adopt_registry()` hook (a heal that cannot be verified
is rolled back — see [08-architecture.md](08-architecture.md)); every
surface's `doc_id` lookup heals a `RegistryMissError` through the same
path. The original typed error propagates when healing is off, the
caps/cooldown are spent, or the harvest fails. One more audited rebuild:
a corrupt `state/governor_state.json` fails soft to fresh counters at
governor construction — which re-arms the request caps — and, when
healing is on, records a `governor-state-rebuild` event in
`state/healing.jsonl` so the reset is visible.

**Force a full re-bootstrap:** delete `state/token_cache.json`. (A DTSG
rejection auto-invalidates it too, and `fbk logout` invalidates it on
confirmed teardown.)

## 4. `fbk whoami` — the canary

Validates the session and prints identity + token availability. One
harmless bootstrap + identity read proves the whole pipeline — jar →
transport → bootstrap → token harvest — before any real run fires. Human
output (synthetic values):

```
$ fbk whoami
state    : logged_in
user     : Example User (12345678901234)
revision : 1047963790
dtsg     : harvested
preloads : 12 queries
```

Token values are never printed: the payload carries only HMAC fingerprints
(`<redacted:<12-hex>>`, see §9) plus the sorted cookie-name list. Exit 0
when `LOGGED_IN`, 1 otherwise — a jar needing re-auth is a failed
precondition, not an error. `--no-journal` runs it without writing a
`state/whoami.jsonl` journal line.

## 5. `fbk state` — classification only

The cheap variant of `whoami`: same single bootstrap, **no identity echo** —
prints only the state classification and the evidence markers that produced
it. Built for scripts that gate on session health alone:

```
$ fbk state --no-journal
{"state": "logged_in", "markers": ["CurrentUserInitialData", "DTSGInitData"]}
```

Exit 0 when `LOGGED_IN`, 1 otherwise. Note this is a **network command** —
it classifies from a page bootstrap; treat it as wire-touching when planning
governor budgets (a fresh token cache serves it without a request, but never
rely on that in a plan).

## 6. `fbk logout` — server-side teardown

Session teardown never migrated to GraphQL (research doc:
docs/05-legacy-ajax-and-rest-endpoints.md §8): `logout` rides the classic
form POST to `logout.php` (research doc: docs/02-endpoint-surface-map.md
§2.12) with exactly the documented triple — `fb_dtsg`, `h` (the harvested
per-session `logout_hash`), and `jazoest` (the derivable classic-form
checksum). Flow:

1. One **fresh** homepage bootstrap — the token cache never round-trips
   `logout_hash`, so a cached entry can never authorize a teardown; logout
   always pays one homepage GET.
2. One governed POST with redirects disabled.
3. **Confirmation is a 3xx** — the canonical clean signal is
   `302 → /login` (research doc: docs/14-glossary-and-reference.md §4). On
   confirmation the token cache is invalidated (every cached session token
   is dead post-logout); any other response class is an UNCONFIRMED teardown
   and the cache is kept.

Exit codes: 0 confirmed; 1 unconfirmed; 3 (`NotLoggedInError`) when the
bootstrap carries no logout authorization — the session was already
unusable.

**This is destructive to every session sharing these cookies.** The teardown
is server-side: the browser you exported `cookies.txt` from (and any other
client riding the same `xs`) is logged out too. Use it when retiring a jar or
ending a session cleanly — it is not a per-run verb. Re-auth has two sanctioned
paths: `fbk login` (§10, headless) or a fresh export from a logged-in browser
(login is the highest-scrutiny moment on the surface — the governor paces every
step of it).

## 7. `fbk cookies inspect` — offline jar audit

Taxonomy presence + fingerprints against the research-suite cookie taxonomy
(constants.py; research doc: docs/03-authentication-and-session-model.md
§1.1, §9.1): **auth** (`c_user`, `xs`), **device** (`datr`, `sb` — device
reputation, never fabricate or rotate casually), **info** (`wd`, `dpr`,
`presence`, `fr`, `ps_l`, `ps_n`, `locale` — missing `wd`/`dpr`/`presence`
on an authenticated desktop session is an automation tell). Values are
**never printed** — only presence and salted-hash fingerprints. Human output
(synthetic fingerprints):

```
$ fbk cookies inspect
jar   : ...\cli\cookies.txt (10 facebook.com cookies)
auth  : complete (c_user + xs)
  [auth  ] c_user     present  fp=1a2b3c4d5e6f
  [auth  ] xs         present  fp=0f1e2d3c4b5a
  [device] datr       present  fp=a1b2c3d4e5f6
  [device] sb         present  fp=6f5e4d3c2b1a
  [info  ] wd         present  fp=f1e2d3c4b5a6
  ...
other : none
note  : flat jar: expiry not tracked (session jar owns expiry policy)
```

Exit contract:

| Condition                          | Exit |
|------------------------------------|------|
| Auth pair (`c_user` + `xs`) complete | 0    |
| Pair missing (stderr says which)    | 1    |
| Jar missing / unparseable / no facebook.com rows (`CookieLoadError`) | 7 |

Because it touches nothing but the jar, this is the natural **precondition
check in scripts and jobs** — run it before any live batch and gate on the
exit code.

## 8. Global flags

Every (sub)command accepts the common flag set (src/commands/common.py).
Flags are typed in full — prefix abbreviation is disabled tree-wide, so a
renamed flag fails loudly instead of silently surviving as an abbreviation.

| Flag              | Semantics |
|-------------------|-----------|
| `--root ROOT`     | Package root override; CLI equivalent of `FBK_ROOT` (see [02-configuration.md](02-configuration.md) §1) |
| `--cookies COOKIES`| Jar path override; CLI equivalent of `FBK_COOKIES` |
| `--no-journal`     | Disable request journaling for this run — no `state/*.jsonl` write. The journal is otherwise named after the command (`state/feed.jsonl`, `state/messenger.jsonl`, …) |
| `--json`           | Raw JSON only. Default mode prints the human summary first, then a final one-line compact JSON of the same payload — scripts can always pipe the last line |
| `--retry N`        | Retry on `RateLimitedError` up to N times with jittered exponential backoff (base 2 s, doubling, cap 30 s, ±50 % jitter, 0.25 s floor — src/retry.py). The only sanctioned retry class: session, checkpoint, registry, cookie, and fingerprint failures never clear on their own and map straight to their codes |
| `--dry-run`        | Build and print each GraphQL request plan **without sending**: every call constructs its complete plan (variables redacted), prints it, and stops before touching the transport, governor, journal, or request counter. The **first planned call ends the run** with exit 0 (`dry-run complete — 0 requests sent`). Raw-seam commands (`upload`, `video upload`) bypass GraphQL with hand-built HTTP and **refuse**: exit 1. The bootstrap still runs governed so plans carry real tokens and preloads |
| `--debug`          | Global, placed **before** the subcommand. Full tracebacks for unexpected errors instead of the terse `error: …` line. Either way an unexpected exception exits 2, and the traceback goes to stderr — stdout payload data stays pipable |

### Exit-code contract (run_command)

Scripts and CI gates match on these codes; renumbering is a breaking change:

| Code | Meaning |
|------|---------|
| 0    | Success (also the `--dry-run` completion sentinel) |
| 1    | Failed precondition returned by the handler: session not `LOGGED_IN` (`whoami`/`state`), missing cookie pair (`cookies inspect`), unconfirmed mutation (`logout`), a raw-seam command under `--dry-run`, or a typed login failure (`login` — bad credentials, aborted code prompt, unrecognized checkpoint, approval timeout) |
| 2    | Any other GraphQL/wire failure or unexpected exception; argparse usage errors |
| 3    | `NotLoggedInError` — re-auth the jar |
| 4    | `CheckpointError` — halt and contain, do not retry the jar |
| 5    | `RateLimitedError` — soft block; respect-backoff |
| 6    | `RegistryMissError` — run `fbk registry refresh` |
| 7    | `CookieLoadError` — jar missing/unreadable/no auth rows |
| 8    | `FingerprintRejectedError` — the edge rejected the TLS/h2 identity; retrying the same session cannot succeed |
| 130  | `KeyboardInterrupt` |

## 9. Security notes

The discipline is enforced structurally, not by convention:

* **Cookie values never appear in any output** — stdout, journals,
  exceptions, or typed errors. Only names, domains, and salted-hash
  fingerprints (first 12 hex chars of SHA-256 over a fixed application
  salt — stable for correlation across runs, never reversible to the
  secret).
* **Token values are wrapped as secrets** (`pydantic.SecretStr`) throughout
  the bootstrap, so `repr()`/`str()`/logging formatters cannot leak them;
  `whoami` and journals emit `<redacted:<fingerprint>>` stand-ins only
  (research doc: docs/11-opsec-and-session-engineering.md §7).
* **Journals redact at write time** — `JSONLJournal.record` deep-redacts
  every secret-named key (a superset of every cookie/token name the wire
  can carry, including `logout_hash`) before the line hits disk; no caller
  can bypass the mapping.
* **The token cache stores token values but never cookie values**; those
  stay in `cookies.txt` and process memory. `cookies.txt` and `state/` are
  gitignored — never commit account identifiers or jar contents.
* `--json` output is exactly as redacted as the human output; redaction
  happens before emission, not at print time.

## 10. `fbk login` — create the session headlessly

The one command that **creates** a session instead of consuming one. It runs
the same device-based web login a browser performs — GET the login page,
harvest the CSRF pair, POST the credentials, drive whatever 2FA checkpoint the
edge serves — and persists the resulting jar to the cookies path (default
`cli/cookies.txt`, `--cookies`/`FBK_COOKIES`-overridable) so every other
command works immediately. No browser export required; the export path
([01-getting-started.md](01-getting-started.md)) remains the fallback.

Wire grounding (research doc: docs/03-authentication-and-session-model.md §3):
the flow GETs `https://www.facebook.com/login/`, harvests `lsd` (the
`["LSD",[],{"token":…}]` require-frame first, a form-hidden input second) and
derives `jazoest` from it (the byte-sum checksum — research doc:
docs/05-legacy-ajax-and-rest-endpoints.md §4), then POSTs `email`/`pass`/
`lsd`/`jazoest`/`login_source=Comet_Dialog`/`persistent`/`default` to the
device-based `login.php` endpoint. Everything rides a **fresh governed
transport with an empty jar** — the login-page GET absorbs `datr` — so a login
is ~3–8 paced requests: expect about a minute, not a burst.

### The 2FA step

The checkpoint the edge serves back is **walked from the server-given form**
(action URL + hidden fields replayed verbatim — the same server-given-shape
principle as the SSR preload replay, research doc:
docs/15-live-calibration-findings.md §P2-2), not from hardcoded shapes, and the
edge's 2FA choice is handled automatically:

* **Approval notification** — the "did you just log in?" page carries no form:
  fbk polls the same URL (governor-paced; the poll interval stacks on the
  governor's gate) while you accept the notification on a signed-in device, for
  up to `--approval-wait` seconds (default 180). The GET transitions once the
  decision lands.
* **Authenticator TOTP** — a code checkpoint (`approvals_code` input): fbk
  prompts for your code, fills it, and replays the server-given fields.
* **SMS OTP** — the same code-entry shape, classified by the page's markers.
* **Generic continue-style forms** — any other server-given form is replayed
  verbatim with its hidden fields.

Anything else is deliberately refused — checkpoint shapes are integrity flows
that vary per deploy and per account risk state; an unrecognized shape must be
completed in a browser, not guessed at headlessly (see the failure table). At
most **8 checkpoint steps** are driven per run (`MAX_CHECKPOINT_STEPS`).

### Flags

| Flag | Semantics |
|------|-----------|
| `identifier` | Positional, optional: email, phone, or username — prompted when omitted |
| `--password-stdin` | Read the password from stdin's first line instead of the hidden prompt (scripting) |
| `--code CODE` | 2FA code up front (TOTP/SMS) — skips the interactive code prompt when the edge asks |
| `--approval-wait S` | Seconds to wait for the phone-notification approval (default 180) |
| `--force` | Re-login even when the jar already carries the `c_user`/`xs` pair |

The common flag set (§8) applies too — notably `--cookies PATH` (where the jar
is written) and `--no-journal` (the run otherwise journals to
`state/login.jsonl`).

### Examples

Interactive (identifier prompted, password hidden, 2FA prompted as served):

```
$ fbk login
identifier (email / phone / username): you@example.com
password:
[login] GET login page
[login] POST credentials
[login] approval required — accept the notification on a signed-in device (waiting up to 180s)
[login] GET approval poll
logged in — user 12345678901234 (checkpoint: approval, 2 step(s), 3 poll(s))
jar saved -> ...\cli\cookies.txt
verify with: fbk whoami
```

Scripted — password on stdin, code up front (PowerShell:
`Get-Content password.txt | fbk login you@example.com --password-stdin --code 123456`):

```
fbk login you@example.com --password-stdin --code 123456 < password.txt
```

### Output

Exit 0 on success or when already logged in; 1 on typed login failures; 2 on
unexpected errors. Default mode prints the human summary above; `--json`
emits the payload alone:

```json
{"state": "logged_in", "user_id": "12345678901234", "checkpoint": "approval",
 "steps": 2, "polls": 3, "saved_to": "...\\cli\\cookies.txt", "next": "fbk whoami"}
```

`checkpoint` is the **first 2FA variant the edge asked for** (`approval` /
`code-totp` / `code-sms`, `null` on a direct login); `steps`/`polls` count the
checkpoint steps consumed and approval poll rounds. On success the jar is
written atomically in Netscape format (src/transport/cookies.py
`save_netscape`) and the stale `state/token_cache.json` is invalidated —
verify with `fbk whoami`.

**Already-logged-in guard:** when the on-disk jar already carries the
`c_user`/`xs` pair, the command is idempotent — it emits
`{"state": "already_logged_in", "user_id": …, "jar": …}`, prints
`already logged in (user …) — … use --force to re-login`, and exits 0 without
touching the wire. `--force` re-runs the flow over the existing pair.

### Security notes

* The password and 2FA codes are read via getpass/stdin — **never argv** — and
  exist only in process memory; they are never logged, journalled, or printed.
* The `state/login.jsonl` journal records body field **names** only
  (`email`, `pass`, `lsd`, …), never values (the §9 discipline).
* The payload carries identity metadata and checkpoint labels only — never
  credentials.
* The governor paces every step of the flow; a login is the
  highest-scrutiny moment on the surface — expect roughly a minute, and never
  retry it in a loop.

### Honesty: schema-decoded, not live-fired

The state machine is schema-decoded and unit-tested **offline** against
synthetic checkpoint pages (`tests/unit/test_auth_login.py`); the live login is
the operator's call. Checkpoint shapes vary per deploy and per account risk
state — the walker replays whatever form the server actually served and refuses
anything it does not recognize.

### Failure table (exit 1 unless noted)

| Error | Meaning | Remedy |
|---|---|---|
| `BadCredentialsError` | The identifier/password pair was rejected | Check the credentials and retry |
| `LoginUnrecognizedCheckpointError` | The checkpoint served a shape this flow does not handle | Complete this login in a browser, then export the jar |
| `LoginTimeoutError` | The notification was not approved within `--approval-wait` | Approve it on a signed-in device, then retry |
| `LoginError` | Anything else: the empty code prompt aborted, the login page carried no `lsd` token (page shape changed), or the 8-step checkpoint cap exhausted | Read stderr; browser login + jar export remains the fallback |
| unexpected | Any other exception | exit 2 — `--debug` for the traceback |

## Files that feed this guide

- `src/session.py` — the Session facade: composition order, cache-first
  bootstrap (the identity-mismatch and degenerate-page heals), dry-run
  raw-seam guard, the registry heal (`adopt_registry`)
- `src/auth/bootstrap.py` — page bootstrap, token harvest, login-state
  classification, checkpoint signal
- `src/auth/state.py` — the `LoginState` enum
- `src/auth/login.py` — the headless login state machine (lsd/jazoest
  harvest, credential POST, the server-given checkpoint walker, approval
  polling, `MAX_CHECKPOINT_STEPS`)
- `src/auth/logout.py` — the `logout.php` POST pair, `jazoest`,
  `LogoutService`
- `src/token_cache.py` — the persistent token cache (15-min TTL,
  `load_diagnosed` reason tokens)
- `src/healing.py` — the self-healing coordinator (records the
  `token-cache-rebuild` events)
- `src/transport/cookies.py` — Netscape jar load (with the tolerant
  torn-jar reparse), the login hand-off `save_netscape`, `fingerprint`,
  `redact`
- `src/commands/auth.py` — `whoami` / `state` / `logout` handlers
- `src/commands/login.py` — the `fbk login` handler (prompts,
  already-logged-in guard, jar persistence, token-cache invalidation)
- `src/commands/cookies.py` — `cookies inspect` handler
- `src/app.py` — entrypoint, `--debug`, exit mapping
- `src/commands/common.py` — global flags, `emit` stdout discipline,
  `run_command` exit-code contract
- `src/retry.py` — `--retry N` backoff policy
