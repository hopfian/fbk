# 09 — Safety and OPSEC

The operating-discipline doc: what the governor enforces, what the
platform's enforcement signals look like, and what you must do when they
fire. Read this before anything beyond read-only commands. The research
ground truth is docs/10-rate-limiting-and-behavioral-detection.md and
docs/11-opsec-and-session-engineering.md (repo root); this page is the
operator-side discipline.

## Why the governor exists

During live-calibration Phase 8, roughly **47 requests dispatched at a
metronomic 2.4-second mean interval over two minutes** triggered a
server-side session kill that escalated to an account-level
**"automated behaviour" warning** — the rung of the enforcement ladder
where the account itself, not just the session, is flagged. The gap
distribution was the kill signature: near-zero coefficient of variation,
machine-precision inter-arrivals. `constants.ACCOUNT_WARNING_MARKERS`
carries the banner strings the bootstrap now scans every served page
for, so the operator sees the enforcement state without waiting for an
email.

The honest lesson (docs/15-live-calibration-findings.md §P8-1,
docs/16-client-realism-and-detection-landscape.md §4): volume plus
metronomic timing is the only signal that has ever fired on this project,
and **volume-and-pacing discipline made structural is the only proven
protective lever**. The governor (`src/governor.py`) is that discipline
as architecture — no surface, command, or script can opt out by accident.
It is not cloaking; its job is to keep the client's behavioral
fingerprint inside human-plausible envelopes.

## The policy

| Mechanism | Default | Why |
|---|---|---|
| Inter-arrival gaps | lognormal, floor **4 s**, mean **12 s**, CV **0.7** | Heavy-tailed, never metronomic — the exact anti-pattern of the Phase-8 kill. The floor bounds the left tail; the CV keeps the shape human |
| Warm-up curve | first **8 requests** of each day paced at **2x mean** | A session launching at full throughput from a cold start is itself a detectable anomaly |
| Hourly cap | **120 requests / rolling hour** | Aborts batches before they execute |
| Daily cap | **500 requests / calendar day** | Hard ceiling; counters roll lazily at the next gate tick |
| Mutation budget | **40 mutations / day** (separate, much lower than the read caps) | Writes are 1–2 orders scarcer than reads in human traffic; every call whose friendly name ends in `Mutation` debits it |
| Soft block → cooldown | any empty-200, HTTP 403/429, or `RateLimitedError` flips a **persistent 15-minute cooldown** that blocks all subsequent governed calls | Enforcement is answered with disengagement, never escalating retries |
| Checkpoint → disengagement | **6 hours** — a checkpoint is strictly worse than a soft block (the challenge tier), so the cooldown outlasts any plausible challenge window | |
| Quiet hours (diurnal gate) | active window default **`0-24` (permissive)** | A 24/7-active client is the strongest single-account anomaly a scorer can see; the permissive default leaves your own schedule as the coherence-safe choice. Configure a window (e.g. `FBK_GOVERNOR_QUIET_HOURS=9-23`) to confine automation to it |
| `GovernorBlockedError` | raised on cooldown, caps, exhausted mutation budget, or outside the active window | A **STOP signal, not a retry signal** — backing off further is the correct response |
| State persistence | counters and cooldowns persist to `state/governor_state.json` (atomic write) | Caps survive process restarts within the calendar day — a crash must not silently forget discipline exactly when the platform is already suspicious |

The gate order inside a request is deliberate: cooldown → caps → diurnal
gate → pacing sleep → counters → persist. Caps are checked *before* any
sleep, so a blocked batch aborts without burning wall-clock time. One
process-wide instance is shared by the HTTP transport, the GraphQL
client, and the registry-harvest pool, so there is exactly one pacing
queue per process; the lock is held across the pacing sleep so concurrent
callers queue behind it and inter-arrival gaps stay correct under
parallelism.

Check live state with `fbk governor status` (offline — needs no cookies):

```
$ fbk governor status
enabled       : True
requests      : 0 this hour (cap 120) | 493 today (cap 500)
mutations     : 69 today (budget 40)
soft blocks   : 0 seen
cooldown      : inactive
```

Audit any recorded journal post hoc with `fbk governor audit --file NAME`
(offline): it spaces the journal's request timestamps into gaps and
verdicts them — burst-suspect (p95 < 0.25 s), sparse-anomaly (mean >
60 s), metronomic-suspect (gap CV < 0.1 over 10+ gaps — the Phase-8
gaps had CV 0.0), policy-drift, and floor violations. Flags firing is
data, not failure.

## Environment overrides

Every knob is individually overridable via `FBK_GOVERNOR_*` environment
variables, resolved by `GovernorConfig.from_env` in `src/governor.py`.
Exact names, types, and defaults:

| Variable | Type | Default (field) | Meaning |
|---|---|---|---|
| `FBK_GOVERNOR` | flag | enabled | Master switch. The falsy spellings `off`, `0`, `false`, `no` (case-insensitive) disable the gate entirely |
| `FBK_GOVERNOR_MIN_GAP` | float | 4.0 (`min_gap_s`) | Absolute floor between any two requests, seconds |
| `FBK_GOVERNOR_MEAN_GAP` | float | 12.0 (`mean_gap_s`) | Lognormal inter-arrival mean, seconds (warm-up requests run at 2x) |
| `FBK_GOVERNOR_HOURLY` | int | 120 (`hourly_cap`) | Requests per rolling hour |
| `FBK_GOVERNOR_DAILY` | int | 500 (`daily_cap`) | Requests per calendar day — raise this for a deliberate mass operation such as a registry harvest |
| `FBK_GOVERNOR_MUTATION_DAILY` | int | 40 (`mutation_daily_cap`) | Mutations per calendar day |
| `FBK_GOVERNOR_COOLDOWN` | float | 900.0 (`cooldown_s` = 15 x 60) | Soft-block disengagement window, seconds |
| `FBK_GOVERNOR_QUIET_HOURS` | str | `"0-24"` (`quiet_hours`) | Active window in local hours `"H-H"`; `off`/`0-24`/`24`/`always` disable gating; `lo > hi` means an overnight window (e.g. `22-6`) |
| `FBK_GOVERNOR_WARMUP` | int | 8 (`warmup_requests`) | First N requests of a day paced at 2x the mean gap |

Notes, all verified in source:

* A malformed override is **silently ignored in favor of the safe
  default** — a governor that crashed on bad env would take the whole
  transport down with it.
* The gap CV (0.7) is deliberately **not** env-overridable; lowering it
  is how you rebuild the Phase-8 kill signature, so it has no knob.
* **Overrides are for tests and dry-runs, not for production sessions.**
  `FBK_GOVERNOR=off` exists so the offline test suite and scripted
  dry-runs never sleep; disabling the gate on a live session removes the
  only protective mechanism this client has. Do not do it against the
  real edge.

## The mutation budget

Writes are scarce in human traffic, so they get their own daily budget
(default 40) on top of the read caps:

* **What debits it:** every GraphQL call whose friendly name ends in
  `Mutation` — react/unreact, comment, delete-comment, publish, batch
  publishes, privacy saves, group/page actions, messenger sends, story
  creates, and so on. The classification happens in the GraphQL client;
  the debit happens at the transport gate (`post_graphql` with
  `is_mutation=True`), before I/O.
* **What does not:** reads, obviously — but also the rupload media
  transfer plane (chunk plumbing, not a user-visible mutation; the
  GraphQL publish that concludes each upload still debits), and the
  once-per-session `logout.php` POST (debits the read budget — a
  deliberate, documented choice).
* **Read-first workflow:** prefer `--dry-run` (prints the full request
  plan — variables redacted — and sends nothing), read commands, and
  drafts (`fbk draft save`) for composition, then spend mutations
  deliberately. The budget is per calendar day and survives restarts.
* **Check it:** `fbk governor status` shows `mutation_count / budget`
  live. Hitting the cap raises `GovernorBlockedError` with "reads still
  allowed" — the batch aborts, it does not degrade.

## Enforcement response doctrine

**On a soft block — disengage.** The signatures: HTTP 403/429, a 200
with an empty or unparseable body, a `RateLimitedError` envelope. The
governor has already flipped its 15-minute persistent cooldown before
the exception reaches you; every subsequent governed call refuses until
it expires. Your move is to *stop*, optionally extend the window with
`fbk governor cooldown --minutes N`, and let it expire. The containment
principle (docs/11-opsec-and-session-engineering.md §5): enforcement is
met with disengagement, never escalating retries — retrying into a soft
block converts it into a hard one. The only sanctioned retry is
`--retry N`, which applies only to `RateLimitedError`, with jittered
exponential backoff (2 s base, 30 s ceiling, 0.25 s floor) — a scalpel
for transient blips, never persistence.

**On a checkpoint — stop for the day.** A checkpoint is rung 4 of the
challenge ladder, strictly worse than a soft block: the governor
disengages for 6 hours, and the correct human response is to do
something else. Do not retry the jar, do not push through the
challenge with automation. If the session was killed, re-export a fresh
jar from a real browser when you legitimately return — and only after
resolving the challenge in that browser.

**On an account warning — full stop.** If a served page carries any
`ACCOUNT_WARNING_MARKERS` banner ("We suspect automated behaviour",
"unusual activity", restriction/disable language), `fbk whoami`/`state`
will surface it (`ACCOUNT_WARNING:...` markers). Cease automated
activity on the account entirely; that is the ladder's top rung.

## Journals and privacy

Every governed request is journaled to `state/<command>.jsonl`: method,
URL, status, content length, and body field *names* — never values.
**Redaction is at write time and cannot be bypassed:** any value under a
secret-named key (the cookie names `xs, datr, c_user, sb, fr, presence`;
the token names `fb_dtsg, lsd, access_token, sessionid, privacy_write_id`;
plus `logout_hash` and `async_get_token`) is replaced with a salted
SHA-256 fingerprint (`<redacted:<12 hex>>`) before the line touches
disk. Fingerprints are stable across runs — enough to correlate entries
in review, never reversible to the secret.

Why this design: the journals are the study's dataset — versioned,
greppable, safe to keep and commit *because* secrets never enter them.
`--no-journal` exists for the inverse need: runs that must not touch
shared state at all (one-off probes, isolated tests, or any invocation
where you want no local footprint beyond the governor state).

Review your own footprint with the journal family — all offline:

```
fbk journal list
fbk journal show --file feed
fbk journal stats --file feed
fbk governor audit --file feed     # the pacing verdict over the same data
```

## Secrets handling

* `cookies.txt` is the only secret-bearing input and is **gitignored**
  (the tree ships none — re-export from a logged-in browser). Its
  values live in cookies.txt and process memory only.
* The token cache (`state/token_cache.json`) stores only the CSRF token
  pair (fb_dtsg/lsd) and non-secret identity metadata (user id, name,
  revision, TTL stamps) — **never cookie values**. Tokens are wrapped in
  pydantic `SecretStr` so no `repr()`/`str()`/serialization path can leak
  them into logs or journals.
* Journals redact at write (above); `Bootstrap.safe_dict` fingerprints
  token values for the same reason; transport journal entries carry
  field names only.
* Never commit account identifiers, cookie contents, or real
  usernames/ids — in journals, docs, code comments, or issue text. The
  repo's own convention is synthetic ids (`12345678901234`-style)
  everywhere else.
* `state/` is gitignored as a directory (governor state, token cache,
  journals, drafts all live there). Keep it that way in any fork.

## Operating rules of thumb

1. Let the governor pace you — never add your own tight loops, and never
   disable the gate on a live session.
2. Read before you write: `--dry-run` first, reads second, mutations
   last — and spend the 40/day mutation budget like it is your own.
3. `GovernorBlockedError` means STOP. Caps and cooldowns are the
   governor working, not failing.
4. On any soft-block signal: disengage (`fbk governor cooldown`), do not
   escalate. On a checkpoint: stop for the day.
5. Keep one coherent identity: one browser profile, one cookie jar, one
   schedule — `fbk fingerprint` exists to keep the tuple stable.
6. Audit yourself: `fbk governor audit` after any session beyond a
   handful of requests; investigate `metronomic-suspect` immediately.
7. Keep the journals — they are your alibi and your dataset — but never
   let a secret near them, and never commit an identifier.

## Files that feed this guide

* `src/governor.py` (the full `from_env` override table, the policy, the
  Phase-8 rationale)
* `src/transport/session.py` (soft-block detection: empty-200, 403/429)
* `src/auth/bootstrap.py` (checkpoint + account-warning detection)
* `src/retry.py` (the only sanctioned retry class)
* `src/journal/recorder.py` (redaction-at-write, the ALL_SECRETS vocabulary)
* `src/commands/governor.py` (status / cooldown / audit)
* `src/commands/common.py` (`--retry`, `--dry-run`, `--no-journal`,
  the exit-code contract)
* `cli/README.md` (the incident narrative and the setup discipline)
* Research suite (repo root, cited by filename):
  docs/10-rate-limiting-and-behavioral-detection.md,
  docs/11-opsec-and-session-engineering.md,
  docs/15-live-calibration-findings.md,
  docs/16-client-realism-and-detection-landscape.md

Related guides: [08-architecture.md](08-architecture.md) (how the
governor is wired into the transport),
[11-troubleshooting.md](11-troubleshooting.md) (exit codes and failure
remedies), [02-configuration.md](02-configuration.md) (paths and
profiles).
