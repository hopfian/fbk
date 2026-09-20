# 07 — Tooling & diagnostics reference

The complete reference for the tooling and diagnostics families: `doc-ids`,
`registry`, `templates`, `governor`, `measure`, `journal`, `doctor`, and
`completions`. These commands do not fetch content and do not mutate the
account — they inspect and maintain the machinery the content commands depend
on: the persisted-query registry, the captured-mutation assets, the request
governor, and the request journals (which are the study's dataset — research
doc: docs/12-python-tooling-architecture.md §8). Every subcommand here is
offline except `registry refresh` (a live re-harvest), `measure latency` (a
live canary run), and `governor cooldown` (a deliberate state mutation); each
section states its wire/state status honestly.

Global flags (`--root`, `--cookies`, `--no-journal`, `--json`, `--retry`,
`--dry-run`, plus `--debug`/`--version` before the subcommand) are documented
in [03-session-and-auth.md](03-session-and-auth.md) and are not repeated in
the per-command flag tables below. Exit codes are a public interface — the full
contract table lives in [11-troubleshooting.md](11-troubleshooting.md). When a
section needs protocol depth it cites the repo-root research docs by plain
filename (e.g. research doc: docs/13-recon-methodology.md).

## Table of contents

* [The persisted-query registry](#the-persisted-query-registry)
* [fbk doc-ids](#fbk-doc-ids)
* [fbk registry refresh](#fbk-registry-refresh) (LIVE)
* [fbk registry diff](#fbk-registry-diff)
* [fbk registry audit](#fbk-registry-audit)
* [fbk templates list](#fbk-templates-list)
* [fbk templates show](#fbk-templates-show)
* [fbk templates verify](#fbk-templates-verify)
* [fbk governor status](#fbk-governor-status)
* [fbk governor cooldown](#fbk-governor-cooldown)
* [fbk governor audit](#fbk-governor-audit)
* [fbk measure latency](#fbk-measure-latency) (LIVE)
* [fbk measure pace](#fbk-measure-pace)
* [fbk measure report](#fbk-measure-report)
* [fbk journal list](#fbk-journal-list)
* [fbk journal show](#fbk-journal-show)
* [fbk journal stats](#fbk-journal-stats)
* [fbk journal export](#fbk-journal-export)
* [fbk doctor](#fbk-doctor)
* [fbk completions](#fbk-completions)

## Files that feed this guide

* `src/commands/registry.py` — `doc-ids` + the registry family
  (refresh/diff/audit).
* `src/graphql/registry.py` — `DocIdRegistry`: the on-disk registry, its
  priority tiers and fail-soft load semantics.
* `src/graphql/registry_refresh.py` — the live harvest pipeline and the
  `RegistryDiff` vocabulary.
* `src/constants.py` — `KNOWN_MUTATIONS` (the catalog `registry audit`
  cross-references) and the burst/sparse verdict thresholds.
* `src/commands/templates.py` — the captured-mutation census
  (list/show/verify).
* `src/governor.py` — the pacing/budget engine, its config, persistence, and
  the `FBK_GOVERNOR_*` overrides.
* `src/commands/governor.py` — status / cooldown / audit.
* `src/journal/analysis.py` — the post-hoc pacing audit heuristics
  (`governor audit`).
* `src/commands/measure.py` and `src/surfaces/measurement.py` — the
  latency/pace/report harness.
* `src/commands/journal.py` and `src/journal/recorder.py` — journal review
  and the write-time redaction boundary.
* `src/commands/doctor.py` — the environment self-diagnostic.
* `src/healing.py` — the healing engine; the doctor's self-healing check
  reads `state/healing.jsonl` through its own API (`count_since`,
  `last_event`).
* `src/commands/completions.py` — the completion bridge scripts and the
  hidden candidate engine.
* `tests/unit/test_doctor.py`, `tests/unit/test_healing_doctor.py`,
  `tests/unit/test_healing_doctor_fix.py`,
  `tests/unit/test_registry_audit.py`,
  `tests/unit/test_governor_audit.py` — the pinned output/exit contracts.
* Companion guides: [03-session-and-auth.md](03-session-and-auth.md) (global
  flags), [09-safety-and-opsec.md](09-safety-and-opsec.md) (governor policy
  and env overrides), [11-troubleshooting.md](11-troubleshooting.md) (exit
  codes).

## The persisted-query registry

Facebook's web client does not ship GraphQL query text on the wire. Every
operation is a *persisted query*: the browser POSTs only a numeric `doc_id`
plus variables, and the server maps the id to the compiled query it holds for
the current deploy (research doc: docs/04-graphql-protocol-deep-dive.md §2.3).
The friendly name for each operation (`CometModernHomeFeedQuery`,
`CometUFIFeedbackReactMutation`, ...) exists only inside the served JS
bundles, in two live-calibrated shapes (research doc:
docs/15-live-calibration-findings.md §3):

```
__d("<Friendly>_facebookRelayOperation",[],(function(...){a.exports="<doc_id>"}),null);
params:{id:"<doc_id>",metadata:{},name:"<Friendly>",operationKind:"query|mutation"}
```

The fbk **registry** is the harvested snapshot of that mapping: one
`friendly_name → doc_id` pair per operation, cut from the `rsrc.php` bundles
of one specific deploy (the harvest methodology is research doc:
docs/13-recon-methodology.md §2). It lives on disk under `data/` as JSON with
a `unique_pairs` list plus a `revision` deploy tag:

| file | role |
|---|---|
| `data/doc_id_registry_v3.json` | refresh output — freshest ids (currently 1,031 pairs, revision 1047963790) |
| `data/doc_id_registry_v2.json` | the prior harvest (1,028 pairs, revision 1047871790); fallback tier, never overwritten |

Reads load through a priority list — v3 first, then v2, then the legacy
merged/full files if present — with per-file fail-soft: a corrupt-but-present
tier is skipped with a stderr warning naming it, and the typed
`RegistryLoadError`/`RegistryMissError` (exit 6) is raised only when nothing
loads at all (src/graphql/registry.py, `DocIdRegistry.from_assets`). Provenance
travels with the data: every load records which file (`source`) and which
deploy (`revision`) supplied the pairs.

The registry is inherently perishable: Facebook's build pipeline rotates
`doc_id`s on deploy rolls, so a registry cut from one deploy drifts stale
against the next. That drift cycle — spot it offline (`registry diff`,
`registry audit`, `templates verify`), then recover with one live re-harvest
(`registry refresh --save`) — is the workflow this whole section automates
(research doc: docs/13-recon-methodology.md §2; the v3-supersedes-v2-by-merge
convention is research doc: docs/15-live-calibration-findings.md §P5-3).

## fbk doc-ids

Search or list the persisted-query registry: the offline lookup table for
"which doc_id does this friendly name carry in the current harvest?". No
session, no cookie jar, no network — built for inspection and shell
scripting against the registry file on disk.

**Syntax**

```
fbk doc-ids [--search REGEX] [--limit N]
```

**Flags**

| flag | meaning | default |
|---|---|---|
| `--search REGEX` | Python regex (`re.search`) over friendly names, e.g. `'SendMessage.*Mutation'` | none — list all |
| `--limit N` | maximum pairs to show, applied after filtering (sorted by friendly name) | 50 |

**Examples**

```
fbk doc-ids --limit 5
registry: doc_id_registry_v3.json (1031 pairs; showing 5)
{"registry": "doc_id_registry_v3.json", "total_pairs": 1031, "shown": {
  "A2UIAssetImageResolverQuery": "27245006561791131",
  "A2UIPageNameResolverQuery": "28690137397259415",
  "AQUAPageVoiceFollowerInviteDialogContentFragmentQuery": "26229726463365580",
  ...}}

fbk doc-ids --search "SendMessage.*Mutation" --limit 8
registry: doc_id_registry_v3.json (1031 pairs; showing 8)
{"registry": "doc_id_registry_v3.json", "total_pairs": 1031, "shown": {
  "CSChatSupportUserSendMessageMutation": "25376937201891185",
  "MAIBAGraphQLSendMessageV2QueryMutation": "28153307324311738",
  "useCometAIHTSSendMessageMutation": "28137996599166900",
  "useCometAIHTSSendMessageV2Mutation": "38081592568123136",
  ...}}
```

(The `--json` payloads above are trimmed to representative entries.)

**Output notes.** Both modes always report the registry `source` file and
`total_pairs`; the human summary line prefixes the `--json` payload (the
shown pairs). An empty match set is a valid query result, not a failure —
exit 0 either way. A regex that matches nothing prints `shown: {}`.

**Status.** Fully offline: registry JSON only, no session, no governor ticks,
no journal records.

## fbk registry refresh

Re-harvest the doc_id registry from the **current live deploy** — the
recovery action when a deploy roll breaks replays. **This is the one
networked command in the registry family**: it bootstraps the logged-in
homepage, collects the deploy's `rsrc.php` bundle URLs, downloads the
bundles (threaded, one curl session per worker), extracts the
`friendly_name → doc_id` pairs with the two live-calibrated shapes, and
reports the added/changed/removed diff against the stored registry — the
docs/13 §2 workflow (research doc: docs/13-recon-methodology.md §2).

It is the fix for two failure classes: `DocIdStaleError` (exit 2 — the
server no longer knows a doc_id the client sent) and `RegistryMissError`
(exit 6 — a name absent from every registry tier). Without `--save` it is a
read-only probe: harvest, diff, print — nothing on disk changes. With
`--save` the fresh pairs are written to `data/doc_id_registry_v3.json`; the
v2 file is **never overwritten** (v3 supersedes v2 by merge, so the previous
harvest stays intact as the fallback tier).

**Syntax**

```
fbk registry refresh [--save] [--workers N] [--max-bundles N]
```

**Flags**

| flag | meaning | default |
|---|---|---|
| `--save` | write the harvest to `data/doc_id_registry_v3.json` (v2 is never overwritten) | off — report only |
| `--workers N` | concurrent bundle downloads | 6 |
| `--max-bundles N` | cap on bundles fetched | all |

**Examples**

```
fbk registry refresh                 # live probe: harvest + diff, no writes
fbk registry refresh --save          # the recovery action (writes v3)
fbk registry refresh --save --json   # machine-mode full diff for scripts
```

Output shape (labels and fields exactly as a live run prints them; values vary per harvest):

```
harvest revision <deploy-revision> — bundles fetched <N>, errors <N>
added <N>, changed <N>, removed <N>
  + <first added names, up to 5> ...
  ~ <first changed names as name old->new, up to 5> ...
saved: <root>\data\doc_id_registry_v3.json
```

**Output notes.** Exit 0 — the verdict is the diff itself; bundle fetch
failures are reported in-band (`fetch_errors`) rather than as a failed exit.
A blocked pipeline (no bundles in the bootstrap, unusable fetcher) raises
`RegistryRefreshError` → exit 2. Every bundle fetch runs under the request
governor like any other traffic, so a mass harvest respects the same pacing
and daily caps — deliberate mass operations raise `FBK_GOVERNOR_DAILY` for
the run (full override table in
[09-safety-and-opsec.md](09-safety-and-opsec.md)).

**The drift cycle.** Doc ids rotate per deploy: a single build push can
re-number every persisted query at once (ids harvested together die
together). The maintenance loop is: `registry diff` (what changed between
the on-disk harvests) → `registry refresh --save` (harvest the current
deploy) → `registry audit` and `fbk doctor` (confirm the catalog and tiers
are coherent again). Run `refresh` when `diff`/`audit` show drift, not on a
schedule — each harvest is real request traffic against the deploy.

**Status.** LIVE: touches facebook.com (homepage bootstrap + bundle
downloads), requires a loadable cookie jar, paced by the governor. `--save`
is the only disk write; it never touches v2.

## fbk registry diff

Offline diff of the registry versions already on disk — **the pre-flight for
`registry refresh`**. It computes the same diff vocabulary the live refresh
reports (`RegistryDiff`: added / removed / changed over friendly names) but
between the two registry *files* in `data/` (v2 vs v3), so you can see what a
registry version bump means with zero wire calls. Use it before every
`refresh --save` to know whether the stored v3 is already behind the v2
baseline (or whether a re-harvest is even warranted), and after a deploy roll
to size the damage.

**Syntax**

```
fbk registry diff [--limit N]
```

**Flags**

| flag | meaning | default |
|---|---|---|
| `--limit N` | maximum names shown per category in human output | 5 |

**Example** (real run, trimmed)

```
fbk registry diff
old: doc_id_registry_v2.json (1028 pairs, revision 1047871790)
new: doc_id_registry_v3.json (1031 pairs, revision 1047963790)
added 3, changed 20, removed 0
  + CometFeedStoryTranslatedContentQuery, CometFeedTranslationMenuQuery, GroupsCometAnonProfilePopoverQuery
  ~ AdsMgmtCampaignDynamicStoreSourceTier3ServerQuery 28669946582630235->27975732952068644, CometModernHomeFeedQuery 28024744447224397->28044109855291494, ...
```

**Output notes.** Human mode prints counts plus a `--limit` head per
category (`+` added, `~` old→new rotations, `-` removed); `--json` carries
the complete name sets, plus `old`/`new` blocks with each side's `source`,
pair count, and `revision`. Exit 0 — the verdict is the diff itself. A
missing version file (e.g. no v3 saved yet) raises the typed
`RegistryMissError` → exit 6; a corrupt one raises `RegistryLoadError` — the
same exit-6 family.

**Status.** Fully offline: reads the two registry files in `data/`, nothing
else.

## fbk registry audit

Cross-reference the `KNOWN_MUTATIONS` catalog (`src/constants.py` — the
friendly_name → doc_id pairs the mutation surfaces pin) against the loaded
registry, offline. The audit closes the catalog loop: every catalog name must
resolve in the registry to the **same** doc_id the catalog pins.

Why it matters, per verdict:

* `doc-id-mismatch` — the registry maps the name to a *different* id than
  the catalog pins. This is a replay risk: the surfaces resolve mutations
  `KNOWN_MUTATIONS`-first, so a stale catalog id means fbk would send the
  wrong persisted-doc hash at mutation time.
* `missing` — the registry does not know the name at all; it would surface
  live as `RegistryMissError` (exit 6).
* `ok` — catalog and registry agree.

**Syntax**

```
fbk registry audit
```

**Flags.** None beyond the global set.

**Example** (real run, 2026-09-20, trimmed to representative lines)

```
fbk registry audit
catalog: 23 KNOWN_MUTATIONS entries vs registry doc_id_registry_v3.json (1031 pairs, revision 1047963790)
ok              CometUFIFeedbackReactMutation
missing         CometUFIDeleteCommentMutation
ok              useCometUFICreateCommentMutation
...
ok              CometNotificationsBadgeCountQuery
missing         StoriesCreateMutation
missing         useStoriesSendReplyMutation
missing         StoriesSuspenseViewerSheetViewerListV2Query
ok              StoriesSuspenseCardOptionMenuExperimentalWithEntryPointQuery
...
summary: ok 19, doc-id-mismatch 0, missing 4
query catalog: none in constants — the canary Query entry in KNOWN_MUTATIONS is the sole query-side declaration, audited above
recovery: re-harvest with `fbk registry refresh --save` (docs/13 §2)
```

**Output notes.** One status line per catalog entry, then summary counts.
When anything is off, the recovery pointer names the fix
(`registry refresh --save`). `--json` adds per-entry
`catalog_doc_id`/`registry_doc_id`. Two honesty notes baked into the
output: `constants.py` declares no separate query catalog — the canary query
(`CometNotificationsBadgeCountQuery`, the live-verified cheapest read;
research doc: docs/15-live-calibration-findings.md §P2-2) is the only
query-side declaration and is audited with the rest; and mismatches/misses
are **data** (exit 0 — the verdict is the audit), while a registry that
cannot load at all fails through the typed path (exit 6).

The current verdict shape: **19 ok / 0 doc-id-mismatch / 4 missing**. The
four missing names are the never-harvested comment-delete registration
(`CometUFIDeleteCommentMutation` — no bundle harvest has captured it since
the deploy rolled to revision 1047963790; its previous registration
returned `field_exception` 1357010 live before the catalog re-pinned it)
plus the three bundle-decoded story operations (`StoriesCreateMutation`,
`useStoriesSendReplyMutation`, `StoriesSuspenseViewerSheetViewerListV2Query`
— decoded from the stories composer chunk, never present in a registry
harvest). They are expected to clear on the next `registry refresh --save`.

**Status.** Fully offline: catalog constants vs the registry file on disk.

## fbk templates list

Census of the replay assets: every `data/captured_*.json` file, what kind of
capture it is, which mutations it carries, and whether each mutation's
doc_id still resolves in the registry. The mutation engine replays captured
variables verbatim (research doc: docs/15-live-calibration-findings.md
§P2-3 — substitute only target ids and payload text, re-send the encrypted
tracking blobs untouched); this is the command that shows *what* is
captured.

Asset kinds are reported honestly, never forced into one frame: mutations
assets (`captured_mutations.json`, `captured_composer.json`,
`captured_comment_mutations.json`) carry the `mutations` list the loader
reads; `captured_real_deltas.json` is a DGW frame capture and is listed as
what it is, with no mutation checks invented for it. Registry cross-ref
misses are **data**, not errors: three states per mutation — `in registry`;
`in registry, but <name> maps to <other id> (deploy roll?)`; and
`NOT in registry (orphan)` — an orphan is a registry-refresh candidate.

**Syntax**

```
fbk templates list
```

**Flags.** None beyond the global set.

**Example** (real run, trimmed)

```
fbk templates list
data dir : ...\cli\data
registry : doc_id_registry_v3.json (1031 pairs)
captured_comment_mutations.json — 6 mutation(s)
  CometUFILiveTypingBroadcastMutation_StartMutation    9815271091886179  in registry
  useCometUFICreateCommentMutation                   28882491998025365  NOT in registry (orphan)
  ...
captured_composer.json — 3 mutation(s)
  ComposerStoryCreateMutation                         28778531428503134  in registry
  ...
captured_mutations.json — 2 mutation(s)
  CometUFIFeedbackReactMutation                       27646120298312844  in registry
captured_real_deltas.json — delta capture (thread_id=str, sent=str, mqtt_frames=list[0], dgw_frames=list[17])
```

**Output notes.** Exit 0 always — this is the inventory; verdicts belong to
`verify`. An unparseable asset is census data here (`UNPARSEABLE (...)`);
registry absence/corruption raises the typed exit-6 family before output.

**Status.** Fully offline: `data/` assets + registry file; no session, no
network, no cookie jar.

## fbk templates show

Drill down into one captured mutation: print its full variables so you can
see exactly what a replay would send (structure, target fields, tracking
blobs) without a wire call.

**Syntax**

```
fbk templates show --asset FILE --mutation NAME
```

**Flags**

| flag | meaning |
|---|---|
| `--asset FILE` | required — captured asset name under `data/` (e.g. `captured_mutations.json`) |
| `--mutation NAME` | required — the entry's `friendly_name`; the **first match wins** (same-named captures like like-vs-remove reactions are disambiguated at replay time, not here) |

**Example** (real run; trimmed — long values elided)

```
fbk templates show --asset captured_mutations.json --mutation CometUFIFeedbackReactMutation
asset    : captured_mutations.json
mutation : CometUFIFeedbackReactMutation (doc_id 27646120298312844)
variables (secret-named fields redacted via journal redaction; tracking ciphertext truncated):
{
  "input": {
    "feedback_id": "ZmVlZGJhY2s6…",
    "feedback_reaction_id": "1635855486666999",
    "feedback_source": "NEWS_FEED",
    "is_tracking_encrypted": true,
    "tracking": [
      "AZjPesN-3P2qFQoHJMGUvK15nyiBBIYrAfXyJ_gcbl9PxmAE7oYo86RROlaavrkZ…[+1191 chars truncated]"
    ],
    "session_id": "fd0d4d15-2063-4af1-9a4c-cfbbd9f2014f",
    "actor_id": "12345678901234",
    "client_mutation_id": "1"
  },
  "scale": 1,
  ...
}
```

**Redaction — the security boundary.** Display safety rides **entirely** on
`journal/recorder.redact_entry` with the `ALL_SECRETS` vocabulary (the
cookie/token names — `xs`, `fb_dtsg`, `privacy_write_id`, ... — from
constants plus the bootstrap extras): secret-named values never print in any
mode, only `<redacted:<sha256-fingerprint>>` markers do (research doc:
docs/11-opsec-and-session-engineering.md §7). The command adds no redaction
logic of its own and cannot bypass it — `--json` emits the same redacted
structure. Tracking blobs are *not* secret-named — they are opaque ciphertext
replayed verbatim by design — so they print with only their head kept
(first 200 chars plus a `+N chars truncated` length note): enough to identify
which capture is loaded, never kilobytes of dump.

**Output notes.** Exit 0 on success; exit 1 (failed precondition) when the
asset is absent, unparseable, not a mutations file, or no entry matches
`--mutation` — the error names the captured candidates.

**Status.** Fully offline; redaction is enforced in every output mode.

## fbk templates verify

Structural sanity gate over every `data/captured_*.json` asset: parse +
required fields + doc_id registry resolution. The check to run after
restoring or re-capturing assets, and after a deploy roll.

**Syntax**

```
fbk templates verify
```

**Flags.** None beyond the global set.

**Example** (real run, trimmed)

```
fbk templates verify
data dir : ...\cli\data
registry : doc_id_registry_v3.json (1031 pairs)
WARN     captured_comment_mutations.json
  - entry 2 (useCometUFICreateCommentMutation): doc_id '28882491998025365' is an orphan — absent from the registry (refresh candidate)
ok       captured_composer.json — 3 mutation(s)
ok       captured_mutations.json — 2 mutation(s)
ok       captured_real_deltas.json — delta capture: 17 entries, no mutation checks
summary: 4 asset(s) — 0 critical, 1 warning(s) — verify PASS
```

**Output notes.** Per mutations asset, every entry must have a
`friendly_name`, non-empty `variables`, and a doc_id that resolves in the
registry. Entry-shape gaps and orphans are **WARN** lines — data, not
failures. The one CRITICAL condition is an asset that fails to *parse*
(unreadable, invalid JSON, a `mutations` key that is not a list) — that
fails the gate. Exit 0 when every asset parses (warnings allowed); exit 1 on
any critical. Non-mutations captures only have to parse. Registry
absence/corruption raises the typed exit-6 family before any check runs.

**Status.** Fully offline; verdicts: PASS (0) / FAIL (1) with per-asset
status lines.

## fbk governor status

Read the request governor's live state: today's counters against the caps,
the separate mutation budget, soft blocks seen, and cooldown state. The
governor is the pacing/budget engine that gates **every** request fbk sends
— lognormal inter-arrival gaps (floor 4s, mean 12s, cv 0.7), hourly/daily
caps, a much lower mutation budget, warm-up, quiet hours, and enforcement
cooldowns (research docs: docs/10-rate-limiting-and-behavioral-detection.md
§7, docs/11-opsec-and-session-engineering.md §8, and the policy layer in
docs/16-client-realism-and-detection-landscape.md).

Counters persist in `state/governor_state.json` and survive process
restarts within the calendar day; they roll lazily on the next gate tick
(no timer thread). This command reads the state; it never mutates anything.

**Syntax**

```
fbk governor status
```

**Flags.** None beyond the global set.

**Example** (real run)

```
fbk governor status
enabled       : True
requests      : 0 this hour (cap 120) | 493 today (cap 500)
mutations     : 69 today (budget 40)
soft blocks   : 0 seen
cooldown      : inactive
```

`--json` additionally carries `cooldown_remaining_s`, the diurnal window
(`active_window: "0-24"` is the permissive default) and `in_active_window`,
and `warmup_requests` (the first 8 requests of each day run at 2x mean gap).

**Output notes.** Exit 0 always. A disabled governor
(`FBK_GOVERNOR=off`) is a valid configuration, not an error: the payload
reports `enabled: false`. The full override table
(`FBK_GOVERNOR_MIN_GAP`, `_MEAN_GAP`, `_HOURLY`, `_DAILY`,
`_MUTATION_DAILY`, `_COOLDOWN`, `_QUIET_HOURS`, `_WARMUP`, and the
`FBK_GOVERNOR=off` master switch) is in
[09-safety-and-opsec.md](09-safety-and-opsec.md).

**Status.** Fully offline: constructs the governor directly, no Session, no
wire, no state mutation.

## fbk governor cooldown

Manually enter a governor cooldown **now** — the operator-initiated
containment move (research doc: docs/11-opsec-and-session-engineering.md
§5). `--minutes` overrides the configured cooldown length for this
engagement, then the command engages the cooldown exactly as if a soft
block had been seen: every subsequent governed request blocks
(`GovernorBlockedError`) until the window expires. The correct response to
any enforcement signal — or to your own suspicion — is disengagement, never
escalating retries (research doc:
docs/10-rate-limiting-and-behavioral-detection.md §7).

**Syntax**

```
fbk governor cooldown [--minutes MIN]
```

**Flags**

| flag | meaning | default |
|---|---|---|
| `--minutes MIN` | cooldown length in minutes | 15.0 |

**Example**

```
fbk governor cooldown --minutes 30
cooldown set: 30 minutes — automated activity should stop for this window (docs/11 §5 containment)
```

**Output notes.** Exit 0 on success; exit 1 when the governor is disabled
(`FBK_GOVERNOR=off` — nothing to cool down). Confirm the window any time
with `fbk governor status` (`cooldown : ACTIVE — Ns left`).

**Status.** Offline (no wire), but **mutates governor state**: the cooldown
deadline persists to `state/governor_state.json` and binds every later
governed run until it expires. Deliberate, not casual — run it when you
mean to stay off.

## fbk governor audit

Post-hoc pacing self-audit of one request journal — the closed loop the
other two governor commands cannot be (research doc:
docs/11-opsec-and-session-engineering.md §8). It reads a journal's `ts`
sequence back, spaces the **request** entries into inter-arrival gaps, and
verdicts the pacing that *actually happened* against the policy the
governor was configured with: did the run look like the Phase-8 metronome
kill (~47 requests at a metronomic 2.4s mean that triggered a session kill
and an account-level warning — research doc:
docs/15-live-calibration-findings.md §P8-1), or like a human?

**Syntax**

```
fbk governor audit [--file NAME]
```

**Flags**

| flag | meaning | default |
|---|---|---|
| `--file NAME` | journal name under `state/` (without `.jsonl`) | `session` |

**Example** (real run)

```
fbk governor audit --file session
journal       : session (121 request gaps)
gaps          : min 3.580s  p50 10.494s  mean 82.490s  p95 91.399s  max 4791.463s
gap cv        : 5.867  (policy mean 12.0s, floor 4.0s)
verdict       : sparse-anomaly, policy-drift, floor-violations(9)
```

**Output notes.** The verdict flags, with every threshold cited in
`src/journal/analysis.py`:

| flag | fires when | meaning |
|---|---|---|
| `burst-suspect` | p95 gap < 0.25s | machine-precision burst (docs/10 §4 burst window) |
| `sparse-anomaly` | mean gap > 60s | anomalously-sparse signature (docs/10 §8) |
| `metronomic-suspect` | gap CV < 0.1 with ≥ 10 gaps | near-uniform inter-arrivals — the classic bot giveaway; the CV floor is a deliberately conservative heuristic, gated on a minimum gap count |
| `policy-drift` | observed mean outside [0.5, 2.0]× the policy mean | materially hotter or colder than the governor's own target; the upper band matches the warm-up ceiling so warm-up-heavy journals do not false-positive |
| `floor-violations(N)` | N gaps under the configured floor | should be zero by construction — the governor floors every gap; any nonzero count means the discipline was bypassed (e.g. `FBK_GOVERNOR=off` runs, or pre-governor journals) |

Only request entries (carrying `method` + `url`) are spaced — event markers
(`session_start`, `latency_sample`, ...) never inject phantom requests; a
crash-torn trailing line is skipped, never fatal. The policy audited against
is the live governor's config, so `FBK_GOVERNOR_*` overrides apply exactly
as they did at run time. The verdict is **data**: flags firing is the audit
working, not the command failing — exit 0 either way. Exit 1 is the failed
precondition: journal missing, or fewer than two spaced requests (nothing
to audit). Strictly read-only: never mutates governor state, never
journals, never networks. (In the real run above the flags are honest:
that journal spans an idle session with long pauses — sparse — and predates
the current floor policy on 9 gaps.)

**Status.** Fully offline, read-only.

## fbk measure latency

Sequential **live** canary latency run — the docs/10 §8 harness leg that
characterizes response time before any batch escalation window (research
doc: docs/10-rate-limiting-and-behavioral-detection.md §8: canaries gate
batches). `--samples` sequential canary calls, each timed and journaled;
the summary carries min/mean/p50/p95/max.

The default canary is the live-verified cheapest read —
`CometNotificationsBadgeCountQuery` (doc_id 9714526941947209) with
`{"environment": "MAIN_SURFACE"}`: a read-only viewer badge the real web
surface fires constantly, so extra calls are maximally boring to the risk
engine (research doc: docs/15-live-calibration-findings.md §P2-2). Typed
canary errors (`RateLimitedError`, checkpoints, staleness) are counted as
**failed samples, never retried and never aborting the window** — a rate
limit at sample #3 is exactly the ceiling this harness exists to find, and
a retry would flatter the very latency being measured. Live-calibrated
10-sample reference window: min 0.344s / mean 0.602s / p50 0.360s / p95
2.669s (research doc: docs/15-live-calibration-findings.md §P6-4).

**Syntax**

```
fbk measure latency [--samples N]
```

**Flags**

| flag | meaning | default |
|---|---|---|
| `--samples N` | canary calls to time | 10 |

**Example.** Not run for this guide (live canary). Output shape with the
live-calibrated 10-sample reference window substituted for the summary
numbers (research doc: docs/15-live-calibration-findings.md §P6-4):

```
fbk measure latency --samples 10
canary latency (10 ok / 0 failed of 10 samples): min=0.344s mean=0.602s p50=0.360s p95=2.669s max=…
```

**Output notes.** Exit 0 — failed samples report in the `failed` counter;
the run itself only fails on typed errors. Every sample is journaled as a
`latency_sample` event (a `measure` journal under `state/`), so the run is
auditable afterwards with the journal family.

**Status.** **LIVE**: real network calls, paced by the governor, journaled
per sample. Requires a loadable cookie jar.

## fbk measure pace

Emit a lognormal inter-arrival schedule — **dry math only**: no HTTP, no
session, no cookies. Human inter-arrivals are heavy-tailed (quick flurries,
long pauses); fixed or uniform gaps are the classic statistical bot
signature measurable in a hundred events (research doc:
docs/10-rate-limiting-and-behavioral-detection.md §4). This command prints
the planned action times for wiring into your own batch pacing, with
parameters mirroring the governor's gap model (research doc:
docs/11-opsec-and-session-engineering.md §8 scheduler).

**Syntax**

```
fbk measure pace [--actions N] [--mean-gap S] [--cv CV] [--seed N]
```

**Flags**

| flag | meaning | default |
|---|---|---|
| `--actions N` | actions to schedule | 10 |
| `--mean-gap S` | mean inter-arrival gap in seconds | 5.0 |
| `--cv CV` | coefficient of variation of the gaps (lognormal spread; `cv=0` degenerates to constant gaps) | 0.6 |
| `--seed N` | PRNG seed — identical seeds yield identical schedules (reproducible experiments) | 1234 |

**Example** (real run)

```
fbk measure pace --actions 5 --mean-gap 8.0
t=17.687s  action#1
t=39.439s  action#2
t=48.999s  action#3
t=60.151s  action#4
t=65.574s  action#5
```

**Output notes.** Human mode prints one `t=<seconds>  action#N` line per
action (absolute offsets from schedule start); `--json` carries the times
array plus the parameters. The lognormal is parameterized with
`mu = ln(mean_gap)` and `sigma = sqrt(ln(1 + cv²))` so the drawn gaps hold
the target scale and spread. Exit 0 — pure computation; it cannot fail
short of a bad flag value, which argparse rejects with a usage exit.

**Status.** Fully offline and side-effect-free: writes nothing, touches no
state.

## fbk measure report

Pacing verdict for one journal file: the go/no-go check on a journaled run
before an escalated batch. It reads `<state>/<journal>.jsonl` and classifies
the journaled inter-arrival distribution against the docs/10 §4 heuristics
— metronomic/uniform gaps are the bot signature, so the report answers
"does this run's timing look human?" (research doc:
docs/10-rate-limiting-and-behavioral-detection.md §4/§8).

What it computes: entry and request counts (requests are entries carrying
an HTTP status), the status distribution (all classes plus the 2xx subset),
and the inter-arrival gap statistics (count / mean / p95 / max) from the
entries' `ts` fields, then one verdict:

| verdict | fires when |
|---|---|
| `burst-suspect` | p95 gap < 0.25s (the machine-precision burst window) |
| `sparse` | mean gap > 60s, or fewer than two timestamps |
| `healthy` | lognormal-ish human pacing in between |

**Syntax**

```
fbk measure report --journal NAME
```

**Flags**

| flag | meaning |
|---|---|
| `--journal NAME` | required — journal name under the state dir (without `.jsonl`) |

**Example** (real run)

```
fbk measure report --journal stories
journal 'stories': sparse (13 requests, 13 ok-2xx, mean gap 155.245s, p95 gap 1640.770s)
```

**Output notes.** The verdict is data, not a pass/fail flag — exit 0 with
whichever verdict the gaps earn; a missing journal raises a typed
`ValueError` (exit 2) naming the expected path. Two honest caveats vs
`fbk governor audit`: report spaces **every** entry's timestamp (event
markers like `session_start` participate in the gap sequence), and it is the
coarser verdict vocabulary — for the full flag set (metronomic CV,
policy-drift, floor violations) against the governor's own policy, use
`governor audit`. Note also that the command builds the session facade (it
needs the jar to load — exit 7 without one) but **sends nothing**: the only
work is reading the journal file.

**Status.** No network traffic, but session-bound: requires a loadable
cookie jar. Pass `--no-journal` to avoid a `measure` journal file.

## fbk journal list

Enumerate the request journals under `state/` — the entry point for
reviewing the study's own dataset. The journals are the dataset: every
governed HTTP request lands as one JSONL line in a per-command journal
unless `--no-journal` was passed (research docs:
docs/11-opsec-and-session-engineering.md §7,
docs/12-python-tooling-architecture.md §8), and every entry was redacted
**at write time** — the family below just reads that already-safe data
back.

**Syntax**

```
fbk journal list
```

**Flags.** None beyond the global set.

**Example** (real run, 2026-09-20, trimmed to representative rows)

```
fbk journal list
9 journal(s) under ...\cli\state
draft                    2 entries         557 B  2026-09-19T19:40:50Z → 2026-09-19T19:41:32Z
feed                    60 entries       19003 B  2026-09-19T18:20:01Z → 2026-09-20T07:51:23Z
session             128 entries       30819 B  2026-09-19T18:23:15Z → 2026-09-19T21:09:37Z
stories                  14 entries        4341 B  2026-09-20T09:01:27Z → 2026-09-20T09:35:06Z
...
```

**Output notes.** Per journal: name, entry count, size, first→last entry
span (UTC), and a `[torn trailing line]` marker when the file ends without
its final newline — the crash-torn shape the recorder's no-fsync design
accepts (at most one record of forensic continuity, never a secret; the
other subcommands skip such a line with a stderr note). `--json` adds
`size_bytes`, raw `first_ts`/`last_ts` epochs, and `torn_trailing_line` per
file. Zero journals — or a missing state dir — is a **reported fact**
("no journals"), never an error: exit 0 always. This is the command for
auditing the dataset after a scrub.

**Status.** Fully offline: no network, no session, no cookie jar required.

## fbk journal show

Print one journal's entries — verbatim, because they were redacted at write
time (every secret-named value was replaced by a salted fingerprint before
it touched disk; the values only ever live in `cookies.txt`).

**Syntax**

```
fbk journal show --file NAME [--limit N] [--tail]
```

**Flags**

| flag | meaning | default |
|---|---|---|
| `--file NAME` | required — bare journal name, resolved as `state/<NAME>.jsonl` | — |
| `--limit N` | show N entries | 20 |
| `--tail` | show the **last** N entries instead of the first N | off |

**Example** (real run)

```
fbk journal show --file stories --limit 4
journal stories: showing 4 of 14 entries (first 4)
2026-09-20T09:01:27Z  POST 200  https://www.facebook.com/api/graphql/
2026-09-20T09:02:36Z  POST 200  https://www.facebook.com/api/graphql/
2026-09-20T09:02:53Z  POST 200  https://www.facebook.com/api/graphql/
2026-09-20T09:04:47Z  POST 200  https://www.facebook.com/api/graphql/
```

**Output notes.** Human mode renders request entries as
`ts method status url` and event entries (`session_start`,
`latency_sample`, ...) as `ts event <name>`; `--json` carries the raw
entries array. `--file` takes a **bare name, never a path**: names carrying
path separators or dot-segments are rejected before resolution, so no
invocation can point the reader outside `state/`. A valid-but-absent name
is a failed precondition (exit 1, with a pointer to `journal list`); a
torn trailing line is skipped, while interior corruption stays loud
(exit 2).

**Status.** Fully offline; display relies entirely on write-time
redaction.

## fbk journal stats

Aggregate one journal into the pacing-review primitives: surfaces, status
codes, and arrival-gap statistics. Purely descriptive — the sibling
`governor audit` owns verdicts.

**Syntax**

```
fbk journal stats --file NAME
```

**Flags**

| flag | meaning |
|---|---|
| `--file NAME` | required — bare journal name (`state/<NAME>.jsonl`) |

**Example** (real run)

```
fbk journal stats --file stories
journal stories: 14 entries, span 2018.186s
surfaces:
  (none)                   14
status codes:
  200                      13
arrival gaps: n=13 min=0.142s p50=27.576s p95=1640.77s max=1640.77s
```

**Output notes.** Per-surface request counts come from each entry's
`ctx.surface` tag (`(none)` when untagged — older journals predate the
tag), the status distribution from every entry carrying a status, and the
gap summary (min/p50/p95/max) from consecutive entry timestamps. Note the
gap statistics here space **all** entries, event markers included — for
request-only spacing with verdict flags, use `governor audit`. Exit 0 on
success; exit 1 when the name is path-shaped or no such journal exists.

**Status.** Fully offline.

## fbk journal export

Export one journal for external analysis — the dataset's path out
(research doc: docs/12-python-tooling-architecture.md §8): CSV or JSON,
to stdout or an operator-chosen file.

**Syntax**

```
fbk journal export --file NAME [--out PATH] [--format {csv,json}] [--force]
```

**Flags**

| flag | meaning | default |
|---|---|---|
| `--file NAME` | required — bare journal name (`state/<NAME>.jsonl`) | — |
| `--out PATH` | write the export to PATH | stdout |
| `--format` | export format | `csv` |
| `--force` | allow `--out` to overwrite an existing file | refusal (exit 1) |

**Example** (real run, CSV to stdout)

```
fbk journal export --file friends --format csv
ts,method,status,content_length,surface,url
1789844905.2311099,POST,200,634,,https://www.facebook.com/api/graphql/
```

**Output notes.** CSV flattens every entry to the transport's own metadata
columns — `ts, method, status, content_length, surface, url` — exactly the
fields the wire recorder writes, with `ctx.surface` hoisted to its own
column. `--format json` emits the raw entries array, verbatim like
`show --json`. Redaction is **carried, never re-applied**: every entry was
redacted at write time, so the export adds no redaction logic and cannot
weaken the guarantee. `--out` may legitimately point anywhere (that is what
exporting is for), but an existing file is never silently overwritten —
the refusal is exit 1; `--force` is the explicit opt-in. Without `--out`
the body prints on stdout alone, undecorated, so a CSV remains parseable.
A torn trailing line is tolerated like `show`/`stats`.

**Status.** Fully offline; the only write is the operator-chosen `--out`
target.

## fbk doctor

The one-shot, fully offline self-diagnostic: "is my environment whole?"
Run it after a credential scrub + re-auth cycle, before a live session, or
whenever anything in this guide exits 6. Nine checks, each reported
`pass`/`warn`/`fail` with a remediation hint whenever degraded:

| # | check | verifies | fails when |
|---|---|---|---|
| 1 | registry | v3 present + parses, with the v2 fallback tier probed too | v3 missing or corrupt (critical); v2-only degradation is a warn — reads still serve from v3 fail-soft |
| 2 | capture assets | every code-referenced `data/captured_*.json` parses with a non-empty `mutations` list | any referenced asset missing, corrupt, or empty (critical) |
| 3 | client profile | `data/profile.json` coherence (identity tuple — research doc: docs/08-device-fingerprinting-and-datr.md §5) | present-but-unreadable or incoherent (critical); **absent is a warn** — the transport's built-in default profile is coherence-safe by construction |
| 4 | impersonate | the wire impersonation target resolves, via the **local refused-port probe** (a connection to `127.0.0.1:9` — offline, no external traffic; research doc: docs/12-python-tooling-architecture.md §3) | no supported curl_cffi target (critical); a target diverging from the profile's pin is a warn |
| 5 | cookie jar | presence + auth-pair completeness (`c_user` + `xs`) | **never fails** — an absent or partial jar is a warn, the expected clean post-scrub state |
| 6 | state dir | `state/` exists and is writable (probe writes + immediately removes `state/.doctor-probe` — the only write doctor ever performs); `governor_state.json` parses when present | missing or unwritable (critical); a corrupt governor state is a warn — it fails soft to fresh counters and the next persist repairs it |
| 7 | journal dir | informational journal census under `state/` (count only) | never fails |
| 8 | self-healing | the ambient `FBK_HEAL` switch + a read-only census of `state/healing.jsonl` (24 h event count, per-kind breakdown over the closed healing-kind vocabulary, newest event) — read through the healing module's own API | **never fails** — the single warn is a non-empty log that yields zero parseable rows (torn history; the next healing append starts a fresh readable log); a disabled switch (`FBK_HEAL=off`) is a pass — a legitimate operator choice |
| 9 | version | resolved version vs the pyproject declaration; informational | never fails (skipped, not failed, where pyproject.toml is absent) |

**Syntax**

```
fbk doctor [--fix]
```

**Flags.**

| flag | meaning |
|---|---|
| `--fix` | after diagnosing, quarantine corrupt offline-state files (below) — renamed aside, never deleted; every quarantine is logged |

`--json` emits the structured check list (`name`, `status`, `critical`,
`detail`, `hint` per check) plus a top-level `self_healing` readout — and,
when `--fix` repaired something, a top-level `fixed` list (see below).

**Example** (real run, trimmed)

```
fbk doctor
[PASS] registry        doc_id_registry_v3.json 1031 pairs (revision 1047963790); v2 fallback 1028 pairs
[PASS] capture assets  3/3 referenced assets parse (11 captured mutations)
[WARN] client profile  no profile.json — transport defaults in use (...); advisory, not a failure
       hint: freeze a coherent profile once and replay it forever (docs/08 §7 lifecycle)
[PASS] impersonate     resolved chrome136 via the local refused-port probe (127.0.0.1:9, docs/12 §3 — offline, no external traffic)
[PASS] cookie jar      10 cookies, auth pair complete (c_user + xs)
[PASS] state dir       writable; governor state parses (day_count=493, cooldown_until=0.0)
[PASS] journal dir     9 journal file(s) under ...\cli\state
[PASS] self-healing    healing on; 0 event(s) in 24h (...\cli\state\healing.jsonl)
[PASS] version         2.2.0 (resolved version matches the pyproject declaration)
doctor: 9 checks — 8 passed, 1 warned, 0 failed — environment healthy
```

With `--fix` on an environment carrying a corrupt state file, the same run
quarantines it after the checks (trimmed):

```
fbk doctor --fix
[WARN] state dir       writable, but governor_state.json is corrupt (...) — ...
       hint: no action needed: the next successful governor persist repairs the file
[FIXED] governor_state.json   quarantined -> governor_state.json.corrupt-1789844905
doctor: 9 checks — 8 passed, 1 warned, 0 failed — environment healthy
```

**The `--fix` pass (offline self-repair).** After reporting what it found,
doctor quarantines every corrupt offline-state file it can detect — three
candidates: `governor_state.json` and `token_cache.json` when unparseable
as JSON, and `healing.jsonl` when it is non-empty yet yields zero
parseable rows (the same warn the self-healing check reports — a
row-based JSONL test, not a whole-file `json.loads`). Quarantine is a
**rename aside to `<name>.corrupt-<epoch>`** — never a deletion, so the
operator keeps the evidence — and each quarantine is logged as a
`doctor-fix` event into the fresh healing log (the healing log's own
event is appended after its rename, into the new file). The next run
starts clean: the loaders' fail-soft regeneration owns recovery (a fresh
governor persist, a fresh bootstrap, a fresh log). Parseable files are
left untouched, and the pass never touches `data/` — the registry heal
is network-side and stays governed. It is best-effort: a failed rename
is skipped, never fatal, and the exit code stays decided by the checks
alone (0/1, unchanged by `--fix`).

**Output notes.** The final line carries the counts and the verdict
(`environment healthy` / `environment broken`). The self-healing check's
human line reports the switch state (`healing on` / `healing off (FBK_HEAL)`),
the 24 h event count, the log path, and the newest event's kind when one
exists; `--json` additionally carries the top-level `self_healing` payload:
`enabled` (bool), `log` (path), `events_24h` (int), `by_kind` (per-kind
census over the closed vocabulary — `registry-refresh`, `doc-id-retry`,
`token-cache-rebuild`, `transport-retry`, `governor-state-rebuild`,
`doctor-fix`), and `last` (the newest event
row, `null` when the log is absent or nothing parses). Exit semantics (pinned by
`tests/unit/test_doctor.py`): **0 while every critical check passes —
warns allowed, even critical ones; 1 as soon as any critical check
fails.** The jar check is deliberately non-critical so doctor passes on a
healthy offline environment with no jar exported. The offline contract: no
Session, no journal records, no governor ticks, no contact with any
facebook.com surface — the impersonate probe's `127.0.0.1:9` connection is
local by design.

**Status.** Fully offline; the only filesystem write is the state-dir probe
file, removed immediately by the same probe.

## fbk completions

Emit the shell completion bridge script for `fbk`. One positional argument,
no common flags (a script generator emits byte-pure code on stdout — `--json`
would corrupt it, and the parser rejecting the flag documents that louder
than a flag that silently lies).

**Syntax**

```
fbk completions SHELL        # SHELL = bash | zsh | powershell
```

**Installation** — the standard pattern per shell; each emitted script ends
with its own install hint:

* **bash**: source the script from your `~/.bashrc` —
  `source <(fbk completions bash)`
* **zsh**: source it from your `~/.zshrc` —
  `source <(fbk completions zsh)` (the script registers via `compdef` when
  available)
* **PowerShell**: add to your `$PROFILE` —
  `fbk completions powershell | Out-String | Invoke-Expression`

**How it works.** Two parts, both offline. The emitted bridge script shells
out to a hidden candidate engine, `fbk _complete -- <words...>`, passing
the words of the command line being completed (the last word is the partial
token; `--` lets partial flags like `--js` get through argparse). The engine
walks the **real** argparse tree — rebuilt at invocation time via
`app.build_parser()` — so suggestions can never drift from the actual
command surface:

* root level → the public command families;
* family level → that family's verbs (`status`, `cooldown`, `audit`, ...);
* verb level → the flags the reached parser accepts plus the common
  plumbing flags (`--root --cookies --no-journal --json --retry --dry-run
  --help`; `-h` and `--version` are suppressed as noise).

Hidden verbs (underscore-prefixed — currently just `_complete`) are never
candidates. Known v1 limits, by design: no flag-*value* completion and no
positional-choice completion — the walker matches names only.

**Status.** Fully offline and side-effect-free; writes nothing (the
install step redirects stdout where your shell wants it).

---

For the failure modes these commands help diagnose — exit 6 registry
misses, `DocIdStaleError` after deploy rolls, governor blocks — see
[11-troubleshooting.md](11-troubleshooting.md). For the governor policy
behind `governor status`/`cooldown`/`audit` and the `FBK_GOVERNOR_*`
overrides, see [09-safety-and-opsec.md](09-safety-and-opsec.md).
