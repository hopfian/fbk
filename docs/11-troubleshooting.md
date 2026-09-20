# 11 — Troubleshooting

Exit codes first — they are a stable public interface (scripts and CI gates match on
them), then symptom → cause → remedy for the failures operators actually hit. Ground
truth: `src/commands/common.py` (`run_command`), `src/app.py`, `src/governor.py`,
`src/graphql/errors.py`, `src/auth/state.py`, `src/auth/bootstrap.py`,
`src/transport/session.py`, `src/commands/doctor.py`, `src/healing.py`.

## Exit codes

| rc | meaning | operator response |
|---:|---|---|
| 0 | Success. Also the `--dry-run` success sentinel: the request plan was printed and nothing was sent. | — |
| 1 | Failed precondition, decided by the handler itself: an unconfirmed mutation (e.g. messenger send with no echoed rows, events create/delete without an id, settings verify-password rejected, logout unconfirmed), `whoami`/`state` classifying anything but `logged_in`, `cookies inspect` finding the auth pair missing, `doctor` failing a critical check. Also the `--dry-run` refusal from raw-seam commands (`upload`, `video upload`): they bypass GraphQL and cannot be planned. | Read the command's stderr; fix the precondition. |
| 2 | Any other `FBGraphError` (unclassified wire failures, `GraphQLProtocolError` variable-coercion rejections, `DocIdStaleError`), any unexpected exception, argparse usage errors, and `GovernorBlockedError` escapes (caps / cooldown / quiet-hours — the governor's STOP signal, a plain `RuntimeError`, not a typed family). | See the sections below; never blind-retry. |
| 3 | `NotLoggedInError` — session invalid/expired. The client already tried one auto re-bootstrap on DTSG rejection and it failed. | Re-export the jar (below). |
| 4 | `CheckpointError` — integrity challenge. The governor has already engaged a 6-hour disengagement. | Halt. Do not hammer. |
| 5 | `RateLimitedError` — soft block. The governor has already engaged a 15-minute cooldown. | Respect-backoff (`--retry N`) or disengage. |
| 6 | `RegistryMissError` (incl. `RegistryLoadError`, its corrupt-file subclass) — friendly_name absent from every registry tier. | Auto-heals first (one capped re-harvest + re-resolve); the manual re-harvest `fbk registry refresh --save` remains the move when the typed error still surfaces (see the registry-drift section). |
| 7 | `CookieLoadError` — jar missing, unreadable, or carries no parseable auth rows. | Re-export the jar. |
| 8 | `FingerprintRejectedError` — the edge rejected the TLS/h2 identity itself, or no supported curl_cffi impersonation target exists. | Fix the fingerprint (below); retrying the same session cannot succeed. |
| 130 | `KeyboardInterrupt` — operator interrupt. | — |

`--retry N` wraps the whole handler and retries **only** `RateLimitedError` — the one
typed error that clears on its own — with jittered exponential backoff, up to N times.
Session, checkpoint, cookie, fingerprint, and registry failures never clear on their own
and map straight to their exit codes without retry (a registry failure with `--retry 3`
burns exactly one call and still exits 6).

## Session dead / logged out

**Symptom.** `fbk state` reports `logged_out`; `fbk whoami` exits 1; live commands raise
`NotLoggedInError` → exit 3. `fbk cookies inspect` may still show a complete auth pair
(the jar exists but the `xs` binding is dead server-side).

**Cause.** The session cookie jar expired or was rotated server-side — the `xs` binding
no longer maps to the `c_user` identity. Wire triggers are the `1357004` "Not logged in."
envelope and the DTSG-rejection pair `1357051`/`1677047`; the GraphQL client cures the
DTSG pair automatically with one re-bootstrap + retry, so an escaped `NotLoggedInError`
means the refresh failed too.

**Remedy.**

```
fbk cookies inspect     # confirm the jar still carries c_user + xs
fbk state               # classify: logged_out vs checkpoint vs shadow_suspect
```

Re-export a fresh Netscape jar from a logged-in browser to `cli/cookies.txt`, then confirm
with `fbk whoami` (exit 0 = `logged_in`). If `state` reports `checkpoint`, see the next
section — do not just re-export and continue.

## Checkpoint

**Symptom.** `CheckpointError` → exit 4; `fbk state` reports `checkpoint`; the wire carries
`CHECKPOINT_REQUIRED`-labeled envelopes (`1384000`-family) or a 302 redirect landing on
`/checkpoint`.

**Cause.** An account or device integrity challenge. The authoritative signal is the
`is_checkpointed:true` flag in the served page's config frames; the governor has already
answered with a 6-hour disengagement cooldown (`observe_checkpoint`).

**Remedy.** Halt. Do not hammer through it — pushing through a checkpoint escalates
account-level enforcement (research doc: docs/11-opsec-and-session-engineering.md).
Verify the cooldown state offline:

```
fbk governor status
```

Do not run live commands until the challenge is resolved in a real browser and the
cooldown has expired. `fbk governor cooldown --minutes N` can extend the disengagement if
you want to stay off longer.

## RateLimitedError vs GovernorBlockedError

Two different "no" signals with opposite responses:

* **`RateLimitedError` (exit 5)** — the *server* pushed back (HTTP 403/429 at the edge,
  `RATE_LIMITED_SUSPECTED` envelopes, or the 200-but-empty soft-block body). The governor
  engages an automatic 15-minute cooldown. This is the one error `--retry N` covers:
  the handler is re-invoked after a jittered exponential backoff, up to N times.

  ```
  fbk feed read --retry 2
  ```

* **`GovernorBlockedError` (exit 2, with the governor's message on stderr)** — *your own
  client* refused the request before it was sent: an hourly/daily/mutation cap is
  reached, a cooldown is active, or the quiet-hours window excludes the current hour.
  This is a STOP signal. Never retry it; see the caps section below.

## Soft block (empty-200)

**Symptom.** Requests return HTTP 200 with an empty or unparseable body; the bootstrap
raises the page-unusable error; `fbk governor status` shows `soft_blocks_seen` incremented
and `cooldown_active: true`.

**Cause.** The edge served a 200-but-degraded response — the documented soft-block
signature (research doc: docs/10-rate-limiting-and-behavioral-detection.md). The
transport and GraphQL layers observe the signal and the governor engages the automatic
15-minute cooldown.

**Remedy.** Do nothing for 15 minutes. Check the remaining time offline:

```
fbk governor status
```

If you saw the signal yourself (in a journal, in `fbk measure report`) and want to
disengage harder, enter a manual cooldown:

```
fbk governor cooldown --minutes 30
```

Every subsequent request raises `GovernorBlockedError` until the deadline passes — that
is the containment working, not a fault.

## Caps reached

**Symptom.** `GovernorBlockedError` with one of these messages (exit 2):
`cooldown active for Ns`, `hourly cap reached (120/h)`, `daily cap reached (500/day)`,
`mutation budget exhausted (40/day) — reads still allowed`, or `outside active window`.

**Cause.** The governor's counters, persisted in `state/governor_state.json`, hit a cap.
Counters roll lazily on the next gate tick: the hourly bucket resets on the next hour
boundary, the daily counters on the next calendar day. The mutation budget is separate —
when only it is exhausted, reads still pass.

**Remedy.**

```
fbk governor status    # see exactly which counter is at its cap
```

Wait for the bucket to roll, or adjust the policy for a specific run via the
`FBK_GOVERNOR_*` environment overrides (caps, gaps, quiet hours, warm-up — full table in
[09-safety-and-opsec.md](09-safety-and-opsec.md)). `FBK_GOVERNOR=off` disables the gate
entirely and is intended for tests and dry runs only — do not use it against the live
edge. Hand-editing `governor_state.json` is a bad idea: a corrupt file fails soft to
fresh counters — silently forgetting discipline exactly when the platform is already
suspicious. The discard is now an audited event: when healing is enabled, the governor
records a `governor-state-rebuild` row in `state/healing.jsonl` ("counters reset to
zero; caps re-arm (audited)") — a counter reset re-arms the request caps, and that is
exactly the kind of change an operator must be able to see.

## Registry drift after a Facebook deploy

**Symptom.** Commands start failing with `DocIdStaleError` (`1570245`-family "Query with
id ... not found", exit 2) or `RegistryMissError` (exit 6). Typically many surfaces at
once — ids harvested together die together on a single build push.

**Cause.** A deploy rolled the `doc_id`s of the persisted queries fbk replays.

**Remedy.** Both failure classes now **self-heal first** (src/healing.py): one capped,
cooled-down re-harvest of the current deploy, then — for `DocIdStaleError` — exactly one
retry on the fresh id (and only when it differs from the rejected one), and for
`RegistryMissError` a re-resolve against the fresh registry. The heal also covers
corruption, not just staleness: a registry whose **every tier is corrupt**
(`RegistryLoadError`) auto-heals through `Session.registry` the same way, and
`Surface.doc_id` — the chokepoint every surface resolves doc-ids through — heals its
misses identically. On success the operator sees the `[heal]` mirror lines on stderr and
the command completes; the typed error never surfaces:

```
[heal] registry-refresh: doc_id registry re-harvested (verified) — pairs=1031; added=3 changed=20 bundles=60 errors=0
[heal] doc-id-retry: doc_id 28024744447224397 rejected for 'CometModernHomeFeedQuery' (1570245) — retrying once with the fresh id 28136951115834703
```

The heal verifies itself: before it rewrites `data/doc_id_registry_v3.json` it backs the
current file up bit-for-bit to `data/doc_id_registry_v3.prev.json` (that file is the
pre-heal backup — the last verified registry, safe to keep or diff); after the harvest the
reloaded registry must clear 200 pairs (`MIN_HARVEST_PAIRS` — the shipped v3 carries
1,031). A degenerate or unparseable harvest — a soft-blocked or shape-drifted page parse —
is refused and **rolled back**: the backup restored via `os.replace`, or the fresh v3
dropped when no previous file existed (resolution falls back to v2 exactly as before the
heal). The rollback events an operator may see:

```
[heal] registry-refresh: degenerate harvest refused — rolled back — 3 pairs < floor 200; added=0 changed=0 bundles=60 errors=0
[heal] registry-refresh: harvest verification failed — rolled back — JSONDecodeError(...)
```

The typed error still surfaces — and the manual `fbk registry refresh --save` remains the
move — exactly when:

* `FBK_HEAL=off` (or `0`/`false`) — healing is disabled for the process;
* the **cooldown is active** — a heal already re-harvested within the last
  `FBK_HEAL_REGISTRY_HOURS` (default 6), so the re-harvest is refused and the original
  error propagates;
* the **harvest failed** — the `[heal] registry-refresh: re-harvest failed — ...` line
  shows why, then the original typed error follows;
* the **backup could not be created** — no overwrite without a rollback path:
  `[heal] registry-refresh: re-harvest skipped — no backup possible`, then the original
  error;
* the name is **lazy-loaded outside the homepage harvest's reach** — a `RegistryMissError`
  that survives a fresh registry (extend the harvest per research doc:
  docs/13-recon-methodology.md §2);
* **same-id shape drift** — the fresh registry carries the *same* id for the rejected
  name, so the retry is refused: that is a caller bug (the request shape, not staleness),
  and re-firing the identical body fails identically.

Diagnose offline either way:

```
fbk registry diff       # offline pre-flight: v2 vs v3 pair counts and changes
fbk registry audit      # offline: KNOWN_MUTATIONS cross-ref against the registry
```

Recovery is the live re-harvest:

```
fbk registry refresh --save
```

`--save` writes `data/doc_id_registry_v3.json`; the v2 fallback tier is never overwritten.
Re-run `fbk doctor` afterwards to confirm both tiers parse; its self-healing check also
summarizes what the auto-heals did (the `state/healing.jsonl` census — see
[07-reference-tooling.md](07-reference-tooling.md)).

## Doctor failures

`fbk doctor` exits 1 when any **critical** check fails; warns never fail the run (an
absent jar is the expected post-scrub state). Read the per-check line and its hint:

| check | fails when | remedy |
|---|---|---|
| registry | v3 missing/corrupt | `fbk registry refresh --save` (or restore the file); v2-only degradation is a warn — reads still serve from v3 |
| capture assets | a `data/captured_*.json` is missing, corrupt, or has an empty mutations list | restore the capture asset from the repo |
| client profile | profile.json present but unreadable or incoherent | repair or delete `data/profile.json` — a broken profile must never reach the wire |
| impersonate | no supported curl_cffi target (probe to 127.0.0.1:9 — offline) | upgrade curl_cffi (exit 8 is the live-run equivalent) |
| cookie jar | never fails — absent/partial jar is a **warn** | re-export from a logged-in browser |
| state dir | missing or not writable (probe writes and removes `state/.doctor-probe`); a corrupt `governor_state.json` is a warn — it fails soft to fresh counters and the next persist repairs it, with the rebuild audited (`governor-state-rebuild`) | `mkdir` it or fix permissions; `fbk doctor --fix` quarantines the corrupt state file |
| journal dir | informational, never fails | — |
| self-healing | never fails — a non-empty but unparseable `state/healing.jsonl` is a **warn** (torn history); `FBK_HEAL=off` is a pass, a legitimate operator choice | no action needed — the next healing append starts a fresh readable log |
| version | informational, never fails | — |

### `fbk doctor --fix` — the offline quarantine pass

Adding `--fix` runs a repair pass after the checks: corrupt offline-state
files are quarantined — **renamed aside to `<name>.corrupt-<epoch>`,
never deleted** (the evidence is kept) — and every quarantine is logged
as a `doctor-fix` event into the (fresh) `state/healing.jsonl`. What it
touches:

* `state/governor_state.json` — unparseable JSON; the next governor
  persist writes a fresh file;
* `state/token_cache.json` — unparseable JSON; the next bootstrap
  regenerates it;
* `state/healing.jsonl` — non-empty with **zero parseable rows** (the
  same warn condition the self-healing check reports). JSONL-vs-JSON
  nuance: the healing log's corruption test is row-based, not a
  whole-file `json.loads`.

Parseable files are left untouched, and nothing here touches `data/` —
the registry heal is network-side and stays governed. `--json` gains a
top-level `fixed` list (only when `--fix` and something was fixed —
`{"file", "quarantined_to"}` entries); human mode prints `[FIXED]` lines
after the checks. The exit code is unchanged — still 0/1, decided by the
checks alone; the fix pass is best-effort and cannot break the run.

## Cookies inspect exits 1

**Symptom.** `fbk cookies inspect` prints the taxonomy report and then exits 1 with
`error: auth pair incomplete (missing ...)`.

**Cause.** The jar loaded but `c_user` and/or `xs` is absent — a failed precondition, not
an exception. (A jar that is missing/unreadable/has no facebook.com rows raises
`CookieLoadError` → exit 7 instead.)

**Remedy.** Re-export the full jar from a logged-in browser; the pair is validated as a
unit server-side. Confirm with `fbk cookies inspect` (exit 0) and `fbk whoami`.

## Token-cache staleness

**Symptom.** A run that should cost one request re-bootstraps, or stale-token behavior
persists across invocations within the 15-minute window.

**Cause.** `state/token_cache.json` holds the bootstrap tokens (`fb_dtsg`, `lsd`) plus
identity metadata with a 15-minute TTL; entries harvested in a non-`logged_in` state are
never trusted. The GraphQL client already auto-refreshes on DTSG rejection — a persistent
problem means the cache disagrees with a changed session. A **corrupt** cache file is now
self-healed too: `TokenCache.load_diagnosed` classifies the miss (`absent`/`expired`/
`stale-state`/`corrupt`/`invalid-shape`), a `corrupt` or `invalid-shape` file is discarded
— the discard IS the heal — and the bootstrap that follows regenerates it, with a
`token-cache-rebuild` event recorded in `state/healing.jsonl` (see
[03-session-and-auth.md](03-session-and-auth.md) §3).

**Remedy.** Delete the cache to force a fresh bootstrap on the next invocation (a corrupt
file needs no manual delete — the self-heal discards it; deleting is for staleness and
identity-mismatch cases):

```
Remove-Item state\token_cache.json     # (or: rm state/token_cache.json)
fbk whoami                             # re-bootstrap + confirm
```

The cache never stores cookie values — deleting it is always safe; the next run just pays
one extra homepage bootstrap.

## Dry-run as a request-plan debugger

When a command fails at the wire, inspect what it *would have sent* before burning budget
on retries:

```
fbk feed read --dry-run
```

The first planned GraphQL call prints its complete request plan — friendly_name, doc_id,
endpoint, mutation classification, and the variables (secret-bearing values redacted to
salted fingerprints) — then the run ends with exit 0 and `dry-run complete — 0 requests
sent`. Nothing touches the transport, the governor counters, or the journals. Use it to
verify variables shape (a `GraphQLProtocolError` 1675012 variable-coercion failure is a
caller bug and will fail identically on retry) and to confirm which doc_id a surface
resolves. Raw-seam commands (`upload`, `video upload`) cannot be planned and refuse with
exit 1 under `--dry-run`.

## Exit-code quick reference for scripts

```
0   success (incl. dry-run complete)
1   failed precondition (handler-decided; raw-seam dry-run refusal)
2   other FBGraphError / unexpected exception / usage error / GovernorBlockedError
3   NotLoggedInError        — re-auth the jar
4   CheckpointError         — halt, 6h disengagement
5   RateLimitedError        — backoff / 15min cooldown
6   RegistryMissError       — auto-heal first; manual: fbk registry refresh --save
7   CookieLoadError         — jar missing/unreadable
8   FingerprintRejectedError — fingerprint rejected; retrying same session is futile
130 KeyboardInterrupt
```
