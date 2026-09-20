# 01 — Getting started

Install fbk, provide a session cookie jar, verify the environment, and make your
first read-only calls. No mutation is needed at any point in this guide.

## Prerequisites and install

* Python 3.13 recommended (`requires-python >=3.11`).
* Install from the `cli/` directory:

  ```
  pip install -e .
  ```

  This registers the `fbk` console script (entry point `app:main`). From an
  uninstalled source tree, `python -m src` is the same entrypoint.
* Dependencies are `curl_cffi` (browser-coherent TLS/h2) and `pydantic`;
  `websockets` (extra: `realtime`) is needed for `messenger listen`.

Verify the install:

```
fbk --version
```

## The cookie jar

Live (network) commands need a session cookie jar in Netscape format. The
primary way to obtain it is `fbk login` — the headless interactive login
(identifier, hidden password prompt, then the 2FA step the edge serves):

```
fbk login you@example.com
```

The jar is written to the cookies path and `fbk whoami` verifies it; details,
flags, and failure remedies are in [03-session-and-auth.md](03-session-and-auth.md)
§10. Export-from-browser remains the alternative:

1. Log in to facebook.com in a normal browser.
2. Export the cookies as a Netscape `cookies.txt` jar — either from the browser
   DevTools application/storage tab (copy the facebook.com rows into the
   Netscape tab-separated format) or with a cookie-export extension that
   writes the format directly.
3. Place the file at `cli/cookies.txt` (or point `--cookies PATH` /
   `FBK_COOKIES` at it).

Requirements:

* The `c_user` and `xs` cookies **must** both be present — the pair is
  validated as a unit server-side. An incomplete pair is a failed
  precondition, not a partial session.
* `cookies.txt` is gitignored; the shipped tree carries no jar. Never commit
  cookie contents.

The offline introspection commands (`doc-ids`, `registry diff`/`audit`,
`config`, `cookies`, `journal`, `doctor`, `templates`,
`governor status`/`audit`, `measure report`) need no jar at all — you can run
everything in this guide except `fbk login`, `whoami`/`overview` before you
have one.

## First-run ritual

Run these in order. The first is fully offline; the second is your first
networked call.

### 1. `fbk doctor` — offline environment self-diagnostic

```
fbk doctor
```

Eight checks, each reported pass/warn/fail with a remediation hint when
degraded: registry tiers (v3 + v2 fallback), capture assets, client profile,
impersonation target, cookie jar, state-dir writability, journal census, and
version consistency. Exit 0 while every critical check passes (warns are
allowed — an absent jar is the expected clean post-scrub state); exit 1 as
soon as any critical check fails. `--json` emits the structured check list.

### 2. `fbk whoami` — first networked call

```
fbk whoami
```

One bootstrap + identity read that proves the whole pipeline — jar → transport
→ bootstrap → token harvest. Prints the login state, user name and id, deploy
revision, DTSG availability, and preload count. Token values never print;
only HMAC fingerprints. Exit 0 when the session classifies `logged_in`; exit 1
otherwise (re-auth the jar).

### 3. `fbk config show`

Offline. Prints the resolved runtime configuration: root/data/state paths,
cookies path and presence, profile, impersonation target, request timeout,
GraphQL endpoint, and the governor caps.

### 4. `fbk governor status`

Offline. Prints today's counters against caps (120/hour, 500/day,
40 mutations/day), soft-block count, cooldown state, the active window, and
the warm-up request count.

## A read-first walkthrough

Read-only commands are the safe way to learn the tool. Mutations (react,
comment, send, publish, ...) consume a separate, much smaller 40/day budget;
reads only burn the 500/day request cap.

```
fbk feed read
fbk feed read --json
fbk notifications badge
fbk overview
```

* `fbk feed read` — fetch the feed head. `--pages N` walks N pages with a
  jittered `--page-gap` between them; `--page-gap SECONDS` defaults to 2.0.
* `fbk feed read --json` — the same call in machine mode (raw JSON only).
* `fbk notifications badge` — the unseen-count badge query, the cheapest live
  read.
* `fbk overview` — the aggregate dashboard: identity, badges, presence, feed
  head, and thread list in one report.

Expect the governor to pace every request: gaps are lognormal (floor 4s, mean
12s, cv 0.7), the first 8 requests of a day run at 2x mean gap (warm-up), and
multi-request commands visibly wait between calls. That is by design — do not
fight it.

## Global flags

Every command accepts the common plumbing flags (from `fbk <cmd> --help`):

| flag | meaning |
|---|---|
| `--root ROOT` | project root (default discovered / `FBK_ROOT`) |
| `--cookies COOKIES` | Netscape cookies.txt path (default `<root>/cookies.txt`) |
| `--no-journal` | disable request journaling for this run |
| `--json` | machine output: raw JSON only |
| `--retry N` | retry on RateLimitedError up to N times with exponential backoff (default 0) |
| `--dry-run` | build and print each GraphQL request plan without sending anything (first planned call ends the run; raw-seam commands refuse) |

Plus two top-level flags that go **before** the subcommand:

| flag | meaning |
|---|---|
| `--debug` | full tracebacks for unexpected errors instead of the terse error line |
| `--version` | print the version and exit |

Output convention: by default every command prints a human-readable summary
followed by one final compact one-line JSON — scripts pipe the last line. With
`--json`, the indented raw payload alone is printed. Error text goes to stderr
only; payload data stays on stdout, so the two are always cleanly separable.

## Shell completions

```
fbk completions powershell
fbk completions bash
fbk completions zsh
```

Emit the completion bridge script for your shell; redirect it into your
shell's completion mechanism.

## Where to go next

* [02-configuration.md](02-configuration.md) — env vars, path discovery, the
  fingerprint profile.
* [03-session-and-auth.md](03-session-and-auth.md) — the session state
  machine, whoami / state / logout / cookies in depth.
* The reference guides: [04](04-reference-feed.md) feed surfaces,
  [05](05-reference-content.md) content surfaces,
  [06](06-reference-people.md) people surfaces, [07](07-reference-tooling.md)
  tooling.
* [09-safety-and-opsec.md](09-safety-and-opsec.md) — the governor policy in
  full. Read this before your first mutation.
* [11-troubleshooting.md](11-troubleshooting.md) — exit codes and failure
  remedies.

## Operating discipline

The governor paces every request — no surface or script can opt out by
accident. Treat `GovernorBlockedError` as a STOP signal, not a retry signal:
it means a cap is reached, a cooldown is active, or the quiet-hours window
excludes the current hour. Enforcement signals are answered with
disengagement: a soft block (rate limit / empty-200) engages an automatic
15-minute cooldown, a checkpoint disengages for 6 hours. If you need to
disengage on your own initiative, use `fbk governor cooldown --minutes N`
rather than retrying through enforcement. The full policy, including the
`FBK_GOVERNOR_*` override envariables, is in
[09-safety-and-opsec.md](09-safety-and-opsec.md).
