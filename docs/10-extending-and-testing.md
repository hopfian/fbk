# 10 — Extending and Testing

Contributor guide for `fbk`: how command families are wired, how a raw Facebook
operation becomes a calibrated CLI feature, how the test suite pins the wire
contract, and the documentation-sync rules that keep the operator guides true.

Everything here is grounded in the code under `cli/src/` and `cli/tests/`. The
research background lives in the repo-root research suite (`docs/01` through
`docs/16`, cited by plain filename throughout); this file never links to it
relatively — the filenames are the contract.

The operator-facing companion guides are the sibling files in this directory
(`cli/docs/02-...` through `cli/docs/09-...`). This file is the contributor
map: where each behavior is implemented, and what must move with a change.

---

## 1. Adding a command family

A family is three coordinated pieces plus one wiring line. Study
`src/commands/stories.py` and `src/surfaces/stories.py` as the exemplar pair
before writing anything.

### 1.1 The pattern

1. **A surface service** — `src/surfaces/<family>.py`, a class extending
   `surfaces.base.Surface`. This is the only place wire knowledge lives.
2. **A command module** — `src/commands/<family>.py`, exposing
   `register(sub: argparse._SubParsersAction) -> None` plus one `cmd_*`
   handler per subcommand.
3. **Registration** — the module name goes into `_COMMAND_MODULES` in
   `src/app.py` (which also owns `--version`, UTF-8 stream reconfiguration,
   and tree-wide `allow_abbrev=False` via `_disallow_abbrev`).
4. **Tests** — offline unit tests against the stubs in `tests/fakes.py` plus
   the real `data/` captures (see §3).

The layering rule (docs/12 §2, enforced by the `commands/common.py` hub):
commands import downward (surfaces, session, config); nothing imports
commands. No command touches cookies, transport, or GraphQL directly —
`commands.common` is the only sanctioned bridge.

### 1.2 The command-module skeleton

This is the real shape, reduced to one read subcommand (compare
`cmd_tray` in `src/commands/stories.py`):

```python
"""<Family> commands: the read + lifecycle (docs/02 §N <family> family)."""
from __future__ import annotations

import argparse
from typing import Any

from surfaces.<family> import <Family>Service

from .common import add_common_args, emit, new_session, with_session


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    fam = sub.add_parser("<family>", help="<family> surface (...)")
    fsub = fam.add_subparsers(dest="<family>_command", required=True)

    read = fsub.add_parser("read", help="...")
    add_common_args(read)                 # --root/--cookies/--no-journal/--json/
                                          # --retry/--dry-run
    read.add_argument("--limit", type=int, default=20,
                      help="maximum rows (default 20)")
    read.set_defaults(fn=cmd_read)


def cmd_read(args: argparse.Namespace) -> int:
    """Read <the surface rows>.

    Returns:
        0 — an empty result is valid account state.
    """
    with with_session(new_session(args)) as session:
        rows = <Family>Service(session).read(limit=args.limit)

        def human() -> None:
            print(f"rows: {len(rows)}")
            for r in rows:
                print(f"{r.get('name') or '?'} ({r.get('id')})")

        emit(args, {"rows": rows, "count": len(rows)}, human=human)
        return 0
```

Non-negotiables drawn from the real modules:

* **`dest` discipline** — every family's inner subparser gets its own dest
  (`dest="stories_command"` in stories; `args.command` stays the family name
  and names the journal file).
* **`set_defaults(fn=cmd_x)`** — the handler rides in `args.fn`; `app.main`
  dispatches through `commands.common.run_command(args.fn, args)`.
* **Session lifecycle** — `with_session(new_session(args)) as session:` in
  every handler. The `with_session` adopt-form exists precisely so command
  modules resolve `new_session` through their OWN module namespace — that is
  the seam offline tests monkeypatch (`monkeypatch.setattr(commands.feed,
  "new_session", ...)`). Closing at scope exit releases the pooled curl
  connections.
* **Exit codes** — the handler returns the code; `run_command` is the single
  exception barrier mapping typed errors to the stable contract
  (`_EXIT_CODES` in `commands/common.py`: 3 not-logged-in, 4 checkpoint,
  5 rate-limited, 6 registry-miss, 7 cookie-load, 8 fingerprint-rejected,
  130 interrupt). An unconfirmed mutation returns 1 from the handler itself
  (e.g. a send with no echoed rows); renumbering any code is a breaking
  change.
* **Offline-first** — some families add offline subcommands that never build
  a Session (`fbk measure pace` asserts exactly that in
  `tests/unit/test_app_cli_wiring.py`).

### 1.3 The emit discipline

Every handler funnels output through `commands.common.emit`:

* Default mode: run the `human()` renderer first (decoration), then print
  the compact one-line JSON — scripts can always pipe the last line.
* `--json`: the indented payload alone, nothing else on stdout.
* Error text NEVER flows through `emit`; `run_command` owns stderr, so
  stdout/stderr stay cleanly separable.

Feed-shaped stories (feed, profile, groups, pages) render through the shared
`src/commands/render.py` (`print_stories`, `story_payload`) so a story
prints identically no matter which surface produced it. Start a new shared
renderer there rather than duplicating per family.

### 1.4 The surface-service contract (`src/surfaces/base.py`)

`Surface` holds the shared `Session` and exposes the accessors every service
reads through — never transport wiring:

* `self.client` — the session's GraphQL client (governor-paced,
  DTSG-managed). All wire calls go through `self.client.call(...)`.
* `self.doc_id(friendly_name)` — registry lookup; raises `RegistryMissError`
  (exit 6) on a miss.
* `self._mutation_doc_id(friendly)` — the write-path resolution:
  `constants.KNOWN_MUTATIONS` first (live-verified), registry fallback.
* `self._fetch(url)` / `self._preloads(url)` — the single network seam for
  HTML-harvesting services; offline tests monkeypatch `_fetch` to serve
  fixture HTML.
* `self._find(entries, query_name)` — first preload entry match.
* Module-level helpers shared by 2+ surfaces: `load_template` (the canonical
  captured-mutation loader — deep-copies `variables` out of
  `data/captured_*.json` keyed by `friendly_name`, with an optional `where`
  predicate), `_fill` (placeholder substitution), `_attribution`,
  `_mutation_id` (a fresh uuid4 client dedup nonce per mutation call).

Governor transparency is structural: surfaces hold no pacing logic of their
own — every request funnels through the session's client/transport where
`RequestGovernor` enforces the lognormal gaps and hourly/daily/mutation
budgets (docs/10 §7, docs/15 §P9-1). A surface cannot burst by accident.

### 1.5 Wiring checklist

1. Create `src/surfaces/<family>.py` extending `Surface`.
2. Create `src/commands/<family>.py` with `register()` + `cmd_*` handlers.
3. Add `"commands.<family>"` to `_COMMAND_MODULES` in `src/app.py` —
   anywhere except last (the `commands.completions` entry must stay LAST by
   design: its hidden-verb metavar snapshot is taken at registration time).
4. Add offline unit tests (§3) and, when the family adds live-calibrated
   wire shapes, CALIBRATION NOTES in the surface docstring (§2e).
5. Ship the matching `cli/docs/` edit in the same change (§5).

---

## 2. Adding a new operation — the live-calibration workflow

`fbk` speaks persisted queries only: the wire carries `doc_id`, never query
text, so an operation is usable only after its (friendly_name, doc_id,
variables) triple is recovered and verified. The stories lifecycle
(surfaces/stories.py, decoded 2026-09-20) is the reference execution of the
whole workflow.

### (a) Find the operation name and doc_id

First stop: the harvested registry, offline —

```
fbk doc-ids --search "Stories.*"
fbk registry diff          # offline v2-vs-v3 pre-flight
fbk registry audit         # cross-checks KNOWN_MUTATIONS against the registry
```

The registry is harvested from production JS bundles (13-recon-methodology.md
§2): enumerate `rsrc.php` bundle URLs from served HTML, fetch them politely,
and extract Relay persisted-query registrations. `fbk registry refresh
--save` performs this against the current deploy under the RequestGovernor
and writes `data/doc_id_registry_v3.json` (v2 is never overwritten;
`graphql/registry_refresh.py` carries the two live-calibrated extraction
shapes).

**Bundle archaeology for deferred/lazy operations.** The homepage census
misses operations whose registration lives in chunks the homepage never
loads. The stories create/viewers/reply family was decoded from the
`/stories/create/` composer page's bootloader map — carrier bundles
(`Od_plfFIzpe` / `8RCEteEs629`) that no homepage harvest reaches. Load the
feature's own page, walk its chunk graph, and grep the carriers for
`LocalArguments` declarations and `__d("..._facebookRelayOperation", ...)
` module registrations. Operations found this way may be
**registry-orphaned** (bundle-decoded, absent from every harvested registry
file) — for those, the surface bakes the doc_id as a module constant and
uses it directly (see `VIEWERS_DOC_ID` in surfaces/stories.py).

### (b) Resolve the doc_id at runtime

Resolution order, per the surface contract:

1. **`constants.KNOWN_MUTATIONS`** (`src/constants.py`) — the confirmed
   mutation catalog. Source-tagged verification depth: the first block was
   replayed live end-to-end (docs/15 §P2-3/P3-3/P5-1); the second is
   bundle-harvested and schema-derived. `_mutation_doc_id` prefers these.
2. **The harvested registry** (`graphql/registry.py`, priority v3 → v2 →
   merged → full, per-file fail-soft) for reads and unverified writes.
3. **Baked module constants** for registry-orphaned operations.
4. **Captured template replay** supplies the VARIABLES, not the doc_id:
   `surfaces.base.load_template("captured_mutations.json",
   "CometUFIFeedbackReactMutation")` deep-copies the exact variables from a
   DevTools capture in `data/` — the verbatim-replay invariant (docs/15
   §P2-3/P3-3). Template captures stay byte-pristine (module-level cache +
   deep copy on every load).

New live-verified mutations get a `KNOWN_MUTATIONS` entry with the full
citation (docs/ section + phase), per the commenting standards (§4).

### (c) Decode the variables schema

Two ground-truth sources, in precedence order (docs/15 §P2-2):

* **SSR preloader tuples** — page HTML embeds
  `{actorID, preloaderID, queryID, variables, queryName}` registrations
  carrying the EXACT variables the server itself used. Replaying them
  verbatim is the most faithful, bypass-resistant read available
  (`Surface._preloads` / `_find`; the tray read does exactly this with the
  homepage's `StoriesTrayRectangularRootQuery` entry, falling back to baked
  `DEFAULT_STORIES_TRAY_VARIABLES` when no preload is available).
* **LocalArguments declarations in carrier chunks** — for mutations the
  decoded transformer pipeline tells you the input shape: the stories
  commit declares `LocalArguments {input}` and the pipeline's output gives
  the field set (`audiences`, `source "WWW"`, `logging`, plus the
  media-type block). A JS `undefined` serializes as an OMITTED field, never
  an explicit null — against non-nullable id fields an explicit null is a
  different (and noncoercible) wire value.

### (d) Live-verify — read first, then the mutation

Never ship a shape on bundle evidence alone. The order:

1. Verify the READ live first (structural sanity: the decoded response path
   returns rows under the expected key).
2. For a mutation, print the plan with `--dry-run` (full request, variables
   redacted, nothing sent, nothing debited), then fire the real call
   ONCE under the governor's separate mutation budget (40/day — docs/10 §2;
   the friendly-name `Mutation` suffix routes it there automatically).
3. The **fail-safe coercion gate** is the safety net for step 2's
   inevitable mistakes: malformed input is rejected with code 1675012
   (`missing_`/`noncoercible_variable_value`, docs/15 §4) WITHOUT
   executing — nothing is created on error. Live-verified 2026-09-20 on
   the stories create input: omitting `tracking` or `navigation_data`
   drew 1675012 and no story existed afterward.

### (e) Record CALIBRATION NOTES in the surface module docstring

The established section (see the head of `src/surfaces/stories.py`):
a dated "ground truth" block per discovery leg, each entry carrying the
operation name, doc_id, registry provenance (preload/relayOperation/bundle),
the decoded response path, and every deviation from prior art. Bundle-decoded
legs name the carrier bundles. Every live-derived constant elsewhere cites
its docs/ section + phase per the standards (§4).

### (f) Pin with offline unit tests

The decoded shapes become fixtures in `tests/unit/` asserting exact
structures — see §3 and `tests/unit/test_surface_story_lifecycle.py`:
the create test pins the full decoded input dict field-for-field, doc_id
equality included. If Facebook legitimately changes a shape, re-pin AND
update the CALIBRATION NOTES together.

### The doc_id ROTATION reality

doc_ids rotate with Facebook's build pipeline — per-deploy, not per-epoch.
Observed in-tree: `CometUFIDeleteCommentMutation` rotated with deploy
revision `1047963790`; the old registration returned `field_exception`
1357010 live (the audit's doc-id-mismatch class) until a fresh full harvest
replaced it. The runtime, recovery, and audit loops:

* **Runtime** — a stale doc_id surfaces as `DocIdStaleError` (the
  `DOC_ID_UNKNOWN` family) at exit 2; a missing name as `RegistryMissError`
  at exit 6. Both are typed, never silent.
* **Recovery** — `fbk registry refresh --save` re-harvestes the current
  deploy (governor-paced; raise `FBK_GOVERNOR_DAILY` for a mass run).
* **Audit** — `fbk registry audit` cross-references `KNOWN_MUTATIONS`
  against the loaded registry offline so a stale catalog entry is reported
  before it becomes a live-wire surprise.

---

## 3. Testing conventions

### 3.1 The offline unit suite

```
python -m pytest tests -q
```

Fully offline — 930+ tests, no cookies, no network. It stands on two
pillars:

* **Stubs** (`tests/fakes.py`):
  * `StubSession` — quacks like `Session` for surface services:
    `.graphql` (a `StubGraphQLClient`), `.registry` (the REAL registry
    loaded from `data/`, or hermetic pairs via `registry_pairs=`),
    `.bootstrap()`, `.user_id()`, `.cookies`.
  * `StubGraphQLClient` — canned responses keyed by friendly_name; a value
    may be a dict (merged payload), an Exception (raised — typed errors flow
    through service code), or a list (streamed docs, deep-merged). Records
    every `(friendly_name, doc_id, variables)` call in `.calls` so tests
    can assert the exact wire body. `strict=True` (default) fails on any
    unexpected call.
  * `StubTransport` / `StubTransportResponse` — the transport-plane stubs
    for `GraphQLClient` unit tests; queued responses, recorded posts.
* **Real captures as fixtures** (`tests/conftest.py`): `load_asset()` and
  session fixtures serve the actual bytes in `cli/data/` —
  `feed_page1_sample.json`, `captured_mutations.json`,
  `messenger_ws_full.json`, and the rest — so tests validate against real
  protocol shapes, not hand-waved approximations.

The self-healing layer carries its own pinned suites: `tests/unit/test_healing_engine.py`
(the `KIND_*` vocabulary, per-kind caps, the 6 h cooldown, the log, and the
re-harvest's verification + rollback), `test_healing_graphql.py`
(the client's two heal paths), `test_healing_transport.py` (read-only connection-phase
retries, mutations never retried), `test_healing_wiring.py` (the round-two wiring:
`Session.registry` adopting a healed registry when every on-disk tier is corrupt, the
`Surface.doc_id` chokepoint heal with its dry-run refusal, and the governor's
corrupt-state rebuild event), `test_healing_doctor.py` (the doctor's self-healing
readout), and `test_healing_doctor_fix.py` (the `--fix` quarantine pass). The
login state machine carries its own suite: `tests/unit/test_auth_login.py`
(the lsd+jazoest harvest, the credential POST body, the three 2FA variants
with approval polling, the generic continue replay, unrecognized-checkpoint
conservatism, the step cap, and the jar round trip) — all offline against a
duck-typed transport stub and synthetic checkpoint pages; the live login is
the operator's call.

**Pins, not smoke.** Unit tests pin exact decoded shapes and counts: field
sets, doc_id equality, input structures, human-output lines. When a wire
shape legitimately changes, the pin is re-cut in the same change as the
CALIBRATION NOTES — a test failure after a deploy rotation is the pin doing
its job. CLI-wiring tests (`tests/unit/test_app_cli_wiring.py`) drive the
REAL `app.build_parser()` — the exact object the `fbk` entrypoint uses —
never a hand-rolled parser.

### 3.2 Live-gated integration tests

`tests/integration/` runs only when `FBK_LIVE=1` (the marker is registered
in `pyproject.toml`; without the env var every live item collects as a skip,
never a failure — gated twice, in `tests/conftest.py` and the integration
`conftest.py`). Requirements and contract:

* A real `cookies.txt` and network; the module-scoped `session` fixture
  hard-fails unless the bootstrap lands on `logged_in`.
* **Structural assertions only** — the account's data state varies, so
  empty lists pass whenever the shape is right. No data assertions.
* Read-only except one deliberate net-zero lifecycle
  (`test_react_unreact_lifecycle`: a LIKE applied and immediately removed
  on the operator's own story). Publish/comment/friend/group/messenger
  mutations are NOT exercised live.
* The autouse `human_pacing` fixture sleeps a lognormal inter-arrival gap
  before every live item (per-test deterministic seed) — an unpaced full
  live run is a textbook burst signature and has drawn field_exception
  rejections mid-run in practice (docs/10 §4, docs/11 §8).
* No secrets in assertion messages — only length-bounded heads.

### 3.3 Quality gates — all three green

```
python -X utf8 -m ruff check src tests
python -X utf8 -m mypy src --strict
python -m pytest tests -q
```

`ruff` runs at line-length 100 targeting py311 (rule sets E/F/W/I/N/UP/B/C4/
SIM/RUF; tests ignore E501; `import constants as C` is a sanctioned naming
exception). `mypy` is strict across all of `src/`. A change is not done
until all three are green.

---

## 4. Commenting standards summary

The authority is `agent-commenting-standards.md` (repo-root research docs);
this is the working summary. The standards bind every `.py` file under
`src/`.

* **Module docstring is line 1** — the first string literal, immediately
  after `from __future__ import annotations` when present, no blank line
  between. Mandatory sections: a one-sentence summary; `ARCHITECTURE:`
  (the module's role in the layered architecture, state managed,
  invariants enforced); `CALIBRATION NOTES:` whenever anything was
  live-probed; `SECURITY BOUNDARY:` / `NETWORK BOUNDARY:` when applicable.
* **Google-style docstrings** on every public function/method/class/
  BaseModel, with `Args:` / `Returns:` / `Raises:` populated
  non-trivially — descriptions explain role and constraints, never restate
  the type annotation. `Note:` for documented side effects (governor tick,
  file write); `Example:` for non-trivial factories.
* **Calibration citations are mandatory** for every live-derived constant,
  URL, regex, wire shape, or heuristic: a docs/ section plus a Phase
  annotation (`docs/15 §P2-3`) or bundle reference. This is the audit
  trail that makes the codebase maintainable across doc_id rotations.
  `constants.py` additionally requires a section banner per constant group.
* **Inline comments are restricted to six categories** — never narration:
  1. wire-format constants / protocol magic numbers (with source),
  2. regex patterns and their matched live shapes,
  3. mathematical formulas / algorithmic derivations,
  4. concurrency / thread-safety / race-condition guards,
  5. non-obvious fallback and defensive decisions,
  6. live-calibration deviations from prior art.
* **Prohibited patterns** (rejected in review without exception): docstrings
  restating the signature; comments narrating self-evident code; bare
  wire-format literals without annotation; `TODO` without a phase or issue
  reference; commented-out dead code; hedging language ("maybe",
  "probably", "basically"); missing calibration citations.

---

## 5. Documentation-sync discipline (USER-DOC ANCHOR)

Every module under `src/` carries a `USER-DOC ANCHOR:` line in its module
docstring naming the operator guide that documents it, e.g.:

```
USER-DOC ANCHOR: cli/docs/04-reference-feed.md
```

The rule: **when you change a module's public behavior — flags, output
shape, wire shape, exit codes — the matching `cli/docs/` edit ships in the
same change.** The anchor makes the pairing non-optional: the docstring
names the file whose truthfulness your edit can break.

Full anchor map (src area → operator guide):

| src area | operator guide |
|---|---|
| `config.py`, `transport/profile.py`, `commands/config.py`, `commands/fingerprint.py` | 02-configuration.md |
| `session.py`, `token_cache.py`, `auth/*` (incl. `auth/login.py`), `transport/cookies.py`, `transport/session.py`, `commands/auth.py`, `commands/login.py`, `commands/cookies.py` | 03-session-and-auth.md |
| `surfaces/feed.py`, `surfaces/profile.py`, `surfaces/comments.py`, `surfaces/stories.py`, `surfaces/notifications.py`, `surfaces/memories.py`, `surfaces/saved.py`, `surfaces/presence.py`, `surfaces/overview.py` + their `commands/*` twins | 04-reference-feed.md |
| `surfaces/search.py`, `surfaces/marketplace.py`, `surfaces/video.py`, `surfaces/photos.py`, `surfaces/events.py`, `surfaces/upload.py`, `surfaces/video_upload.py` + their `commands/*` twins (incl. `commands/drafts.py` and the draft store) | 05-reference-content.md |
| `surfaces/messenger.py`, `surfaces/friends.py`, `surfaces/groups.py`, `surfaces/pages.py`, `surfaces/settings.py` + their `commands/*` twins | 06-reference-people.md |
| `graphql/registry.py`, `graphql/registry_refresh.py`, `commands/registry.py`, `commands/templates.py`, `commands/governor.py` (the command), `commands/measure.py`, `commands/journal.py`, `commands/doctor.py`, `commands/completions.py` | 07-reference-tooling.md |
| `app.py`, `commands/common.py`, `commands/render.py`, `surfaces/base.py`, `domain/common.py`, `graphql/client.py`, `graphql/parsing.py`, `graphql/errors.py`, `retry.py`, `stats.py`, `realtime/*` | 08-architecture.md |
| `governor.py` (the engine), `journal/recorder.py`, `transport/session.py` soft-block paths | 09-safety-and-opsec.md |
| `src/healing.py`, the `graphql/client.py` heal paths, the `transport/session.py` retry paths | 08-architecture.md (+09 for policy) |
| `constants.py` (the `KNOWN_MUTATIONS` catalog and all live-derived protocol values) | 07-reference-tooling.md |
| all of `tests/` | 10-extending-and-testing.md (this file) |

A module can anchor to more than one guide when its concerns span them
(`transport/session.py` is documented as architecture in 08 and its
soft-block containment behavior in 09). When a new module lands, its anchor
line lands with it.

---

## 6. Self-containment invariant

`cli/` is fully self-contained — nothing under `src/` or `tests/` may
reference a file outside `cli/` (no `../`-style escapes; the suite's
conftest states the invariant outright and path-handling code is pinned
against traversal — the draft-store, journal, and cookies commands reject
`../cookies`, `sub/dir`, and friends in user-supplied names). Consequences:

* **`data/` is immutable** at runtime: captured wire payloads, the doc_id
  registries (v2/v3), surface fixtures. Nothing writes to it; the registry
  refresh writes only its own new file. The captured shapes stay
  byte-comparable against future harvests — keep it that way.
* **`state/` is mutable**: `governor_state.json`, `token_cache.json`, the
  per-command `*.jsonl` journals. Both directories are gitignored
  alongside `cookies.txt`.
* **Never commit secrets or real identifiers**: no cookie contents, no real
  account ids or message bodies in fixtures or test output. Journals
  redact secret names at write (the `SECRET_COOKIE_NAMES` /
  `SECRET_TOKEN_NAMES` vocabulary in `constants.py`); tests use synthetic
  ids and length-bounded heads. If a live capture must become a fixture,
  scrub identifiers first — synthetic ids only.

---

## 7. Where the ground truth lives

The research suite (repo-root `docs/01`–`16`) is authoritative; consult the
relevant document before touching any API-adjacent module:

| document | what it is for |
|---|---|
| 03-authentication-and-session-model.md | Cookie taxonomy, login-state machine, DTSG/lsd tokens, bootstrap classification |
| 04-graphql-protocol-deep-dive.md | The /api/graphql/ persisted-query wire: request shape, streamed NDJSON responses, error envelope codes |
| 06-messenger-mqtt-protocol.md | The MQIsdp-dialect MQTT-over-WS Messenger bus, framing, topics, thrift deltas |
| 10-rate-limiting-and-behavioral-detection.md | Burst/heuristics detection, soft-block signals, the recovery table the governor encodes |
| 11-opsec-and-session-engineering.md | Operational security: journaling/redaction, containment, per-surface journals, quiet-hours discipline |
| 13-recon-methodology.md | The harvest playbook this CLI's registry tooling implements: bundle enumeration, extraction patterns, diff vocabulary, deploy rotation |
| 15-live-calibration-findings.md | Every live probe's findings, phase by phase (P2-3, P8-1, ...) — the citation target for all CALIBRATION NOTES |

Adjacent: 02-endpoint-surface-map.md (the surface map `_COMMAND_MODULES`
order mirrors), 08-device-fingerprinting-and-datr.md and
09-headers-and-request-signing.md (transport identity),
12-python-tooling-architecture.md (layering and path resolution),
16-client-realism-and-detection-landscape.md (the realism posture), and
agent-commenting-standards.md (§4 above).

---

## Files that feed this guide

- `src/app.py` — parser assembly, `_COMMAND_MODULES`, dispatch
- `src/commands/common.py` — `add_common_args`, `new_session`,
  `with_session`, `emit`, `run_command`, the exit-code contract
- `src/commands/render.py` — shared story renderers
- `src/commands/stories.py` — exemplar command family
- `src/surfaces/base.py` — the `Surface` contract and shared wire helpers
- `src/surfaces/stories.py` — exemplar surface with current CALIBRATION NOTES
- `src/graphql/client.py` — the persisted-query client, typed errors, dry-run
- `src/graphql/registry.py`, `src/graphql/registry_refresh.py` — registry
  loading priority and the re-harvest pipeline
- `src/healing.py` — the self-healing coordinator (caps, cooldowns,
  `state/healing.jsonl`)
- `tests/unit/test_healing_engine.py`, `test_healing_graphql.py`,
  `test_healing_transport.py`, `test_healing_wiring.py`,
  `test_healing_doctor.py`, `test_healing_doctor_fix.py` — the healing
  suites
- `tests/unit/test_auth_login.py` — the offline login state-machine suite
- `src/commands/registry.py` — `doc-ids` / `registry refresh|diff|audit`
- `src/constants.py` — `KNOWN_MUTATIONS` and the calibration-citation model
- `tests/fakes.py` — `StubSession` / `StubGraphQLClient` / `StubTransport`
- `tests/conftest.py`, `tests/integration/conftest.py` — fixtures, live gate,
  human pacing
- `tests/unit/test_surface_story_lifecycle.py` — exemplar shape-pinning suite
- `tests/unit/test_app_cli_wiring.py` — exemplar real-parser wiring suite
- `pyproject.toml` — the three quality gates, live marker, ruff/mypy config
- `README.md` — setup, layout, governor summary
- repo-root research docs: `agent-commenting-standards.md`,
  `13-recon-methodology.md`, `15-live-calibration-findings.md`
