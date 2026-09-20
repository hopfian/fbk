# fbk

**A production-grade, reverse-engineered research client for Facebook's internal web API surface.**

fbk drives the same endpoints a browser does — authenticated GraphQL persisted
queries, the MQTT-over-WebSocket Messenger bus, and the DGW realtime stack —
with a coherent browser TLS/h2 fingerprint, a persistent bootstrap-token cache,
and a request governor that paces every single request. Everything fbk needs at
runtime lives inside this `cli/` directory, and the offline test suite
validates every parser and surface against real captured wire shapes.

```
$ fbk whoami
state            : logged_in
user             : Test User (12345678901234)
deploy           : revision 1047963790
dtsg             : present (fingerprinted)
preloads         : 47 harvested

$ fbk feed read
fetched          : 3 stories
page 1           : has_next_page=True
...
{"fetched": 3, "has_next_page": true, ...}
```

## Highlights

* **35 command families** across the Facebook surface: feed, stories,
  notifications, messenger, groups, pages, events, marketplace, saved items,
  comments, photos, video, search, friends, settings, presence, and more —
  each with `--json` machine output and a compact one-line JSON tail.
* **Fingerprint-coherent transport** — every request (HTTP *and* WebSocket)
  rides a `curl_cffi` chrome impersonation, so the edge sees one consistent
  TLS/h2 identity. The profile is inspectable, freezable, and validated for
  internal coherence (`fbk fingerprint`).
* **The request governor** — lognormal inter-arrival gaps, hourly/daily caps,
  a separate 40/day mutation budget, soft-block cooldowns, checkpoint
  disengagement, quiet hours, and a daily warm-up curve. Pacing discipline is
  structural: no surface or script can opt out by accident.
* **Self-healing** — registry rotation auto-re-harvest, corrupt state
  discard-and-rebuild, read-only transport retries — capped, cooled down,
  and logged to `state/healing.jsonl`.
* **Realtime the way the browser speaks it** — the Messenger MQTT dialect
  (MQIsdp over a fingerprinted WebSocket, Thrift-compact delta decoding) plus
  the DGW transport, both under `fbk messenger listen`.
* **A persisted-query registry** — doc_ids are harvested from deployed JS
  bundles (`fbk registry refresh`), cross-referenced against the catalog of
  known mutations (`fbk registry audit`), and diffed between harvests, because
  Facebook rotates registrations on every deploy.
* **Offline-first testability** — 1250+ unit tests run with no cookies and no
  network, pinning decoders against real wire captures; a separate
  `FBK_LIVE=1` gate covers the live integration suite.
* **Journals as the study dataset** — every networked command appends a
  redacted request journal (`state/*.jsonl`) you can review, audit for pacing,
  and export (`fbk journal`, `fbk governor audit`).

## Install

* Python 3.13 recommended (`requires-python >=3.11`).
* From this directory (`cli/`):

  ```
  pip install -e .
  ```

  This registers the `fbk` console script (entry point `app:main`). From an
  uninstalled source tree, `python -m src` is the same entrypoint.
* Dependencies: `curl_cffi` (browser-coherent TLS/h2) and `pydantic`. The
  `realtime` extra (`websockets`) is only needed for the MQTT WebSocket
  fallback path.

Verify the install:

```
fbk --version
```

## The session

Live (network) commands need a session cookie jar in Netscape format:

1. Log in to facebook.com in a normal browser.
2. Export the cookies as a Netscape `cookies.txt` jar (DevTools storage tab,
   or a cookie-export extension).
3. Place it at `cli/cookies.txt` (or point `--cookies PATH` / `FBK_COOKIES`
   at it).

The `c_user` and `xs` cookies must both be present — the pair is validated as
a unit server-side. The jar is gitignored and the shipped tree carries none;
the offline commands (`doc-ids`, `registry diff`/`audit`, `config`, `cookies`,
`journal`, `doctor`, `templates`, `governor status`/`audit`,
`measure report`) need no jar at all.

First run:

```
fbk doctor        # offline self-diagnostic: registry, assets, profile, jar
fbk whoami        # first networked call: jar -> transport -> bootstrap -> tokens
fbk config show   # resolved paths + governor caps
fbk governor status
```

## Command families

Every command accepts `--root`, `--cookies`, `--no-journal`, `--json`,
`--retry N`, and `--dry-run` (raw-seam commands refuse dry-run); `--debug`
goes before the subcommand. Default output is a human summary followed by one
compact one-line JSON — scripts pipe the last line.

| command | what it covers |
|---|---|
| `whoami` / `state` / `logout` | session validation, login-state classification, server-side teardown |
| `cookies` | jar inspection — taxonomy + fingerprints, never values |
| `feed` | `read`, `paginate`, `react`, `unreact`, `comment`, `delete-comment`, `publish`, `publish-batch`, `edit`, `set-privacy`, `share`, `save`, `notify`, `life-categories` |
| `profile` | `me`, `view` (any profile's timeline feed) |
| `stories` | `tray`, `create` (text/photo/video), `viewers`, `reply`, `presets`, `fonts`, `audience` |
| `notifications` | `badge`, `list` (`--page`/`--all` cursor walks), `mark-seen` |
| `comments` | deep-comments surface: `read`, `react`, `unreact`, `edit`, `delete` |
| `memories` / `saved` | memories feed; saved-items `list`, `save`, `unsave` |
| `messenger` | `threads`, `history`, `send`, `listen` (MQTT bus or `--dgw`) |
| `friends` | `list`, `requests`, `suggestions`, `request`, `cancel`, `accept`, `decline`, `unfriend`, `clear-badge` |
| `groups` | `create`, `join`, `request-join`, `add-members`, `feed`, `post`, `members` |
| `pages` | `create`, `like`, `follow`, `feed` |
| `events` | `list`, `feed`, `create`, `delete` |
| `search` | top-results search (SSR preload replay) |
| `marketplace` | `browse`, `search`, `item` |
| `video` / `photos` | /watch/ feed + badge; own albums + photo grid |
| `upload` | `photo`, `video` — ingest, optionally post |
| `draft` | CLI-local draft store: `save`, `list`, `show`, `publish`, `delete` |
| `presence` / `overview` | chat-visibility status; the aggregate dashboard |
| `settings` | `show`, `set-default-privacy`, `verify-password`, `send-reset-link` |
| `doc-ids` / `registry` / `templates` | persisted-query lookup; `refresh`/`diff`/`audit`; captured-mutation census |
| `governor` / `measure` | pacing state, manual cooldown, journal audit; latency/pace/report |
| `journal` | request-journal review: `list`, `show`, `stats`, `export` |
| `fingerprint` / `config` | TLS/UA profile `show`/`freeze`/`clear`; resolved configuration |
| `doctor` / `completions` | one-shot environment diagnostic; shell completions |

Full usage for every flag: the operator documentation under
[`docs/`](docs/README.md).

## The request governor

Facebook actively flags automated behavior. During one calibration phase,
~47 requests at a metronomic 2.4s mean gap triggered a server-side session
kill and escalated to an account-level "automated behaviour" warning. The
governor (`src/governor.py`) makes volume and pacing discipline structural:

* Inter-arrival gaps are lognormal (floor 4s, mean 12s, cv 0.7) — never
  metronomic. The first 8 requests of a day run at 2x mean (warm-up).
* Caps: 120 requests/hour, 500/day, and a separate 40/day mutation budget.
  Hitting a cap raises `GovernorBlockedError` — a STOP signal, not a retry.
* Any soft-block signal (empty-200, 403/429, rate-limit error) engages a
  persistent 15-minute cooldown; a checkpoint disengages for 6 hours.
* Quiet hours (`FBK_GOVERNOR_QUIET_HOURS`): a 24/7-active client is the
  strongest single-account anomaly.
* Counters persist to `state/governor_state.json`, so caps survive process
  restarts within a day. Every knob is env-overridable via `FBK_GOVERNOR_*`
  (see [docs/09-safety-and-opsec.md](docs/09-safety-and-opsec.md)).

Check state with `fbk governor status`, disengage manually with
`fbk governor cooldown --minutes N`, and audit any journal after the fact with
`fbk governor audit --file NAME`.

The persistent token cache (`state/token_cache.json`, 15-minute TTL) is the
complementary volume lever: one command = one request while the cache is
fresh, instead of re-bootstrapping the multi-megabyte homepage per invocation.

## Documentation

The full operator documentation suite lives in [`docs/`](docs/README.md) —
eleven guides covering install and first run, configuration, the session
model, a complete reference for every command family, the internal
architecture, the governor/safety policy, extending and testing the client,
and troubleshooting (including the exit-code contract).

Every source module carries a `USER-DOC ANCHOR:` line in its docstring naming
the guide that documents it — behavior changes ship with the matching doc
edit.

## Repository layout

```
cli/
  cookies.txt   session jar (Netscape format, gitignored — the tree ships none)
  data/         immutable runtime data: captured wire payloads (scrubbed to
                synthetic content), doc_id registries v2/v3, fixtures
  state/        mutable runtime state: governor counters, token cache,
                request journals (*.jsonl), drafts
  src/          the Python source (flat package layout, top-level imports)
    transport/  curl_cffi session, headers, cookie jar, fingerprint profile
    auth/       page bootstrap, login-state machine, logout
    graphql/    persisted-query client, doc_id registry, parsing, errors
    surfaces/   one class per Facebook surface (feed, stories, messenger, ...)
    commands/   the CLI layer (one module per command family)
    realtime/   MQTT (MQIsdp dialect) + DGW WebSocket transports
    journal/    redacted request-journal recorder + analysis
    governor.py the request pacing engine
  tests/        offline unit suite + FBK_LIVE-gated integration tests
  docs/         the operator documentation suite
```

Performance note: curl_cffi costs roughly 200ms to import, so it is imported
lazily — offline commands never touch that import chain at all.

## Testing

* Unit tests are fully offline — 1250+ tests of canned stub responses plus the
  real captured `data/` fixtures; no cookies or network needed:

  ```
  python -m pytest tests -q
  ```

* Integration tests are cookie-gated: they need a real `cookies.txt` and
  network, and run only when `FBK_LIVE=1` (otherwise they show as skips).

* Lint and types (both are part of the bar; mypy runs strict, and CI enforces
  all three on every push):

  ```
  python -X utf8 -m ruff check src tests
  python -X utf8 -m mypy src --strict
  ```

## Security and privacy practices

* `cookies.txt` and `state/` are gitignored; the token cache never stores
  cookie values; journals redact at write (tokens are fingerprinted, never
  logged in cleartext).
* The committed `data/` fixtures are scrubbed: all third-party personal
  content (names, texts, media URLs, identifiers) is replaced with synthetic
  values, while preserving the protocol shapes the test suite pins against.
* `fbk cookies inspect` never prints cookie values — only taxonomy presence
  and fingerprints.

## License

Copyright (C) 2026 the fbk authors.

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU Affero General Public License as published by the Free
Software Foundation, either version 3 of the License, or (at your option) any
later version. See the [LICENSE](LICENSE) file for the full text.

## Disclaimer

fbk is an independent research client for studying Facebook's web API
surface. It is not affiliated with, endorsed by, or supported by Meta
Platforms, Inc. This codebase ships without any session credentials;
operating it against a live account is done entirely at the operator's own
risk and responsibility, including compliance with the terms of service that
govern the account in use.
