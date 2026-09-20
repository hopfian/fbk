# fbk — operator documentation

fbk is a research client for Facebook's internal web API surface. It drives the same
endpoints a browser does — authenticated GraphQL persisted queries, the MQTT-over-WebSocket
Messenger bus, and the DGW realtime stack — with a coherent browser TLS/h2 fingerprint, a
persistent bootstrap-token cache, and a request governor that paces every single request.
Everything fbk needs at runtime lives inside the `cli/` directory.

These pages are the **operator guides**: how to install, configure, and drive the `fbk`
command. They are distinct from the repo-root research suite (`docs/01`–`docs/16` at the
repository root), which is the ground truth for protocol internals — wire shapes, error
envelopes, antibot landscape, and live-calibration findings. When a guide needs that depth
it cites the research doc by filename, e.g. "research doc:
docs/10-rate-limiting-and-behavioral-detection.md". The numbering used here is independent
of the research suite's 01–16 numbering.

## How to read these docs

| doc | covers |
|---|---|
| [01-getting-started.md](01-getting-started.md) | install, cookies, first run |
| [02-configuration.md](02-configuration.md) | runtime config, env vars, paths, fingerprint profile |
| [03-session-and-auth.md](03-session-and-auth.md) | session model, login/whoami/state/logout/cookies, global flags |
| [04-reference-feed.md](04-reference-feed.md) | feed, profile, comments, stories, notifications, memories, saved, presence, overview |
| [05-reference-content.md](05-reference-content.md) | search, marketplace, video, photos, events, upload, draft |
| [06-reference-people.md](06-reference-people.md) | messenger, friends, groups, pages, settings |
| [07-reference-tooling.md](07-reference-tooling.md) | doc-ids, registry, templates, governor, measure, journal, doctor, completions |
| [08-architecture.md](08-architecture.md) | internal design, request flow, error taxonomy, self-healing |
| [09-safety-and-opsec.md](09-safety-and-opsec.md) | the governor and self-healing policies, request discipline, journals, env overrides |
| [10-extending-and-testing.md](10-extending-and-testing.md) | adding commands/surfaces, live calibration, tests, quality gates |
| [11-troubleshooting.md](11-troubleshooting.md) | exit codes, failures, remedies |

Start at [01-getting-started.md](01-getting-started.md) for install and the first-run
ritual. [11-troubleshooting.md](11-troubleshooting.md) is the reference for exit codes and
failure remedies. [09-safety-and-opsec.md](09-safety-and-opsec.md) explains the governor —
read it before doing anything beyond read-only commands.

## Command → doc map

Every command family from `fbk --help`, mapped to the guide that documents it:

| command | guide |
|---|---|
| `login` | [03-session-and-auth.md](03-session-and-auth.md) |
| `whoami` | [03-session-and-auth.md](03-session-and-auth.md) |
| `state` | [03-session-and-auth.md](03-session-and-auth.md) |
| `logout` | [03-session-and-auth.md](03-session-and-auth.md) |
| `cookies` | [03-session-and-auth.md](03-session-and-auth.md) |
| `feed` | [04-reference-feed.md](04-reference-feed.md) |
| `profile` | [04-reference-feed.md](04-reference-feed.md) |
| `comments` | [04-reference-feed.md](04-reference-feed.md) |
| `stories` | [04-reference-feed.md](04-reference-feed.md) |
| `notifications` | [04-reference-feed.md](04-reference-feed.md) |
| `memories` | [04-reference-feed.md](04-reference-feed.md) |
| `saved` | [04-reference-feed.md](04-reference-feed.md) |
| `presence` | [04-reference-feed.md](04-reference-feed.md) |
| `overview` | [04-reference-feed.md](04-reference-feed.md) |
| `search` | [05-reference-content.md](05-reference-content.md) |
| `marketplace` | [05-reference-content.md](05-reference-content.md) |
| `video` | [05-reference-content.md](05-reference-content.md) |
| `photos` | [05-reference-content.md](05-reference-content.md) |
| `events` | [05-reference-content.md](05-reference-content.md) |
| `upload` | [05-reference-content.md](05-reference-content.md) |
| `draft` | [05-reference-content.md](05-reference-content.md) |
| `messenger` | [06-reference-people.md](06-reference-people.md) |
| `friends` | [06-reference-people.md](06-reference-people.md) |
| `groups` | [06-reference-people.md](06-reference-people.md) |
| `pages` | [06-reference-people.md](06-reference-people.md) |
| `settings` | [06-reference-people.md](06-reference-people.md) |
| `doc-ids` | [07-reference-tooling.md](07-reference-tooling.md) |
| `registry` | [07-reference-tooling.md](07-reference-tooling.md) |
| `templates` | [07-reference-tooling.md](07-reference-tooling.md) |
| `governor` | [07-reference-tooling.md](07-reference-tooling.md) |
| `measure` | [07-reference-tooling.md](07-reference-tooling.md) |
| `journal` | [07-reference-tooling.md](07-reference-tooling.md) |
| `doctor` | [07-reference-tooling.md](07-reference-tooling.md) |
| `completions` | [07-reference-tooling.md](07-reference-tooling.md) |
| `config` | [02-configuration.md](02-configuration.md) |
| `fingerprint` | [02-configuration.md](02-configuration.md) |

## Keeping docs in sync

* Every source module under `cli/src/` carries a `USER-DOC ANCHOR:` line in its module
  docstring naming the `cli/docs/` guide to update when that module's behavior changes.
  If you change a module's user-visible behavior — a flag, an output shape, an exit code,
  a governor default — ship the doc edit for the anchored guide in the same change.
* The repo-root research suite (`docs/01`–`docs/16` outside `cli/`) is the ground truth
  for protocol internals. These `cli/docs/` guides are the operator layer: they cite the
  research docs by plain filename (never relative hyperlinks — they live outside `cli/`)
  and stay focused on operating the CLI.
* Exit codes are a public interface. `cli/docs/11-troubleshooting.md` documents the
  contract table; renumbering an exit code is a breaking change, not a refactor.
* The numbering in `cli/docs/` is independent of the repo-root research suite's 01–16
  numbering. `cli/docs/11-troubleshooting.md` and the research doc
  `docs/11-opsec-and-session-engineering.md` are different documents.
