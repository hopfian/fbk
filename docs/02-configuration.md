# 02 — Configuration, paths, and the transport profile

This guide covers the fbk runtime configuration model: how the package root and
derived paths are resolved (`FBK_ROOT`, `FBK_COOKIES`, `FBK_IMPERSONATE`), the
`cli/` directory contract, and the two offline introspection families that
surface all of it — `fbk config show` and `fbk fingerprint show / freeze /
clear`. Session commands (`whoami`, `state`, `logout`, `cookies inspect`) are
covered in [03-session-and-auth.md](03-session-and-auth.md); the governor's
full override table lives in [09-safety-and-opsec.md](09-safety-and-opsec.md).

Everything here is offline: no session, no cookie jar, no network. Cite
companion research as needed — e.g. research doc:
docs/03-authentication-and-session-model.md (cookie jar), research doc:
docs/08-device-fingerprinting-and-datr.md (coherence matrix), research doc:
docs/12-python-tooling-architecture.md (§4 path resolution, §8
self-containment).

## 1. The resolved-path model

Every invocation — live or offline — starts with one object:
`Config.discover()` (src/config.py). It produces a fully resolved
`Config` whose path fields are absolute. Resolution order, first wins:

1. **`--root ROOT` flag** — seeds `Config.discover`; wins over the env var.
2. **`FBK_ROOT`** environment variable — explicit package root (tests, CI,
   alternate installs).
3. **Module-anchored discovery** — derived from the location of
   `src/config.py` itself, never from the current working directory:
   * editable install: the module lives under `src/`, so
     `Path(__file__).resolve().parents[1]` is `cli/` — the directory
     containing `pyproject.toml`;
   * flat wheel install: the module sits flat in `site-packages` next to the
     shipped `data/`, so `parents[0]` (the site-packages root) is used. The
     layout probe is simply the `src/` directory name check on the module's
     parent.

Anchoring on the package root (not on `cwd`) means every derived path stays
stable no matter where the interpreter is invoked from: run `fbk` from any
directory and it still finds its `data/`, `state/`, and `cookies.txt`.

From the root, the remaining paths derive:

```
root          = <resolved root>
data_dir      = root/data
state_dir     = root/state
cookies_path  = FBK_COOKIES  (or  root/cookies.txt)
profile_path  = root/data/profile.json   (None until the file exists)
impersonate   = FBK_IMPERSONATE           (or  "chrome136")
```

`profile_path` uses present-or-None semantics: `discover()` resolves it only
when the file exists on disk, so "is a profile frozen?" is inherent in the
None-ness — `config show` reports it as a fact.

The `--cookies` flag is applied after discovery (an absolute-path
`model_copy`), so the full precedence is:

| Value        | 1st choice    | 2nd choice     | 3rd choice (default)    |
|--------------|---------------|----------------|-------------------------|
| root         | `--root`      | `FBK_ROOT`     | module-anchored `cli/`  |
| cookies.txt  | `--cookies`   | `FBK_COOKIES`  | `<root>/cookies.txt`    |
| profile.json | —             | —              | `<root>/data/profile.json` |
| impersonate  | —             | `FBK_IMPERSONATE` | `chrome136` (pinned) |

### FBK_IMPERSONATE and the fallback chain

The impersonation target is the curl_cffi TLS/h2 (JA3/JA4) identity. The
default is **`chrome136`** — the target confirmed live 2026-09 to pass the
edge fingerprint gate with zero interstitials across ~40 requests (research
doc: docs/15-live-calibration-findings.md §1). The version is pinned
deliberately: the unversioned `"chrome"` preset would silently rotate the
fingerprint on a curl_cffi upgrade, breaking day-to-day coherence (research
doc: docs/16-client-realism-and-detection-landscape.md §6).

If the installed curl_cffi build cannot express the requested target,
`resolve_impersonate()` (src/transport/session.py) probes the fallback chain

```
chrome136 → chrome131 → chrome124 → chrome120
```

and takes the first candidate the build supports. The probe connects to
`127.0.0.1:9` (a port guaranteed to refuse): an unsupported target raises
`ValueError` before any I/O, a valid one fails with connection-refused after
the TLS options are set. If nothing in the chain works, transport construction
fails with `FingerprintRejectedError` → exit 8.

## 2. The directory contract

```
cli/
  cookies.txt   the session cookie jar — the ONLY secret-bearing input,
                Netscape format, gitignored; the tree ships none
  data/         immutable runtime data — captured wire payloads
                (captured_*.json), doc_id registries v2/v3, surface fixtures
  state/        mutable runtime state — governor_state.json,
                token_cache.json, per-command *.jsonl journals, drafts/
  src/          the Python source (flat package layout)
  tests/        offline unit tests + FBK_LIVE-gated integration tests
```

| Path          | Mutability | Contents                                                              |
|---------------|------------|-----------------------------------------------------------------------|
| `cookies.txt` | operator   | Exported from a logged-in browser; gitignored; never committed.        |
| `data/`       | read-only  | Registries, captures, fixtures. Runtime commands never write here — the one deliberate exception is `data/profile.json`, written only by `fbk fingerprint freeze`. |
| `state/`      | read-write | Governor counters, token cache, request journals, post drafts. Gitignored. |

This is the self-containment contract: nothing outside `cli/` is ever
referenced at runtime, so the directory can be cloned, zipped, and vendored
without environment fixes — copy `cli/` anywhere, drop a `cookies.txt`
inside it, and everything works.

## 3. `fbk config show`

Prints the resolved runtime environment with zero network calls and no
cookies.txt required — the point of the command is to inspect the environment
*after a scrub or re-auth*, when inputs are absent. A missing jar is a
reported fact (`cookies_present: false`), never a failure: the command always
exits 0.

Output is the resolved Config plus the governor's caps (embedded verbatim
from `default_governor().status()` — the vocabulary lives in one place), and
on-disk existence for the two secret-bearing inputs (cookies.txt,
profile.json). Real run:

```
$ fbk config show
root             : C:\Users\<you>\Documents\Project\Triage\fb\cli
data dir         : C:\Users\<you>\Documents\Project\Triage\fb\cli\data
state dir        : C:\Users\<you>\Documents\Project\Triage\fb\cli\state
cookies          : C:\Users\<you>\Documents\Project\Triage\fb\cli\cookies.txt (present)
profile          : none (transport default)
impersonate      : chrome136
timeout          : 30.0s
graphql endpoint : https://www.facebook.com/api/graphql/
governor         : enabled — caps 120/hour, 500/day, 40 mutations/day
```

(the paths reflect wherever the `cli/` checkout lives on your machine;
only the shapes matter)

The default mode also prints a final one-line compact JSON of the same payload
(see `--json` in [03-session-and-auth.md](03-session-and-auth.md) §8); with
`--json` you get the indented JSON alone. Resolution honors `FBK_ROOT` /
`FBK_COOKIES` / `FBK_IMPERSONATE` and `--root` / `--cookies` through the same
`build_config` seam every live command uses — what this command prints is
exactly what the next run acts on.

## 4. The client profile: `fbk fingerprint show / freeze / clear`

The profile is the transport's TLS/UA **identity tuple** — every header-level
axis the edge can cross-check against cookies and IP geo. The lifecycle is
freeze-once-replay-forever: rotating any axis between sessions is a known
scraper signature (research doc:
docs/08-device-fingerprinting-and-datr.md §5, §7). The whole family is
offline — no session, no curl_cffi import, no contact with any facebook.com
surface.

### Profile fields (src/transport/profile.py)

| Field                | Default                                                              |
|----------------------|----------------------------------------------------------------------|
| `impersonate`        | `chrome136` — must stay in the same browser family+major as the UA    |
| `user_agent`         | Chrome 136 on Windows 10 (the shape confirmed live 2026-09)           |
| `sec_ch_ua`          | `"Chromium";v="136", "Not_A Brand";v="99", "Google Chrome";v="136"`   |
| `sec_ch_ua_platform` | `"Windows"` — OS class must match the UA exactly                      |
| `sec_ch_ua_mobile`   | `?0` (desktop) — cross-checked against platform and `wd`              |
| `accept_language`    | `en-US,en;q=0.9` — must agree with the locale cookie and IP geo       |
| `timezone`           | `UTC` (IANA) — must agree with IP geo or carry a plausible story      |
| `dpr`                | `1` — device-pixel-ratio echo of the `dpr` cookie; numeric           |
| `wd`                 | `1280:720` — screen-size echo of the `wd` cookie; `w:h`, both > 0    |
| `notes`              | `{}` — free-form metadata dict                                        |

### What "coherence" means

`ClientProfile.validate_coherence()` encodes the docs/08 §5 cross-axis
matrix — every pair of axes the edge can compare must not contradict:

* UA says `Windows NT 10.0` ↔ `sec_ch_ua_platform` must be `"Windows"`.
* UA says Android ↔ `sec_ch_ua_mobile` must be `?1` and platform
  `"Android"`.
* `sec_ch_ua_mobile=?1` with platform `"Windows"` is incoherent.
* The UA's `Chrome/<major>` must match the Chrome major inside `sec_ch_ua`.
* A **versioned `chrome<N>` pin must agree with the UA's Chrome major** —
  the TLS↔UA axis the edge scores first (research doc:
  docs/09-headers-and-request-signing.md §1.2). Boundary: only versioned
  Chrome pins are major-checked; unversioned (`"chrome"`), non-Chrome, and
  empty targets carry no major this rule can see and stay unguarded here.
* `wd` must be a plausible `w:h` (both positive integers); `dpr` must be
  numeric.

Each problem message names the contradicting axis pair so you can repair the
profile rather than guess.

### `fingerprint show`

Prints the **resolved** profile — the frozen `data/profile.json` when one
exists, otherwise the transport defaults — every field in identity-tuple
order, plus the coherence verdict. **Always exit 0**: a coherence problem
(or an unreadable profile.json) is data reported by an introspection surface,
never a failure. Real run on an unfrozen tree:

```
$ fbk fingerprint show
profile : implicit defaults — not frozen (...\cli\data\profile.json)
impersonate       : chrome136
user_agent        : Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36
sec_ch_ua         : "Chromium";v="136", "Not_A Brand";v="99", "Google Chrome";v="136"
sec_ch_ua_platform: "Windows"
sec_ch_ua_mobile  : ?0
accept_language   : en-US,en;q=0.9
timezone          : UTC
dpr               : 1
wd                : 1280:720
notes             : {}
coherence         : PASS
```

### `fingerprint freeze`

Validate + persist the identity tuple to `data/profile.json`. Flags:

| Flag                    | Effect                                                        |
|-------------------------|---------------------------------------------------------------|
| `--ua UA`               | User agent; sanitized **before** freezing (`HeadlessChrome/<v>` → `Chrome/<v>`, ` Headless` dropped) so the frozen file is byte-what-the-transport-sends |
| `--locale LOC`          | Accept-Language (e.g. `en-US,en;q=0.9`)                        |
| `--tz TZ`               | IANA timezone (e.g. `America/New_York`)                        |
| `--impersonate TARGET`  | curl_cffi target; must agree with the UA's Chrome major        |
| `--sec-ch-ua HINT`      | Client hint; its Chrome major must agree with `--ua`           |
| `--sec-ch-ua-platform PLATFORM` | OS class; must match the UA                                  |
| `--sec-ch-ua-mobile FLAG`      | `?0` desktop / `?1` mobile; cross-checked                    |

Unset flags inherit the class defaults (the same safe tuple the transport
falls back to). **Freeze refuses to write on ANY coherence problem** — exit 1,
nothing written, problems listed — because an incoherent identity must never
be persisted. Illustrative refusal (pin disagrees with the UA major):

```
$ fbk fingerprint freeze --impersonate chrome131
target           : ...\cli\data\profile.json
impersonate      : chrome131
...
coherence        : REFUSED — impersonate target chrome131 disagrees with UA Chrome/136 major
error: refusing to freeze an incoherent profile — an incoherent identity
must never be persisted (docs/08 §7)
```

The gate is `validate_coherence` alone. Enforcement continues at load time:
every live Session constructs its transport through `load_or_default()`,
which **rejects** (not silently downgrades) an incoherent frozen file —
`FingerprintRejectedError`, exit 8. Freezing a coherent profile once and
leaving it alone is the intended lifecycle.

### `fingerprint clear`

Removes `data/profile.json`; already-clear is reported and still exit 0
(idempotent by design). After a clear, the transport falls back to its
built-in default profile.

## 5. Environment variables

| Variable                  | Read by           | Effect                                                                 |
|---------------------------|-------------------|------------------------------------------------------------------------|
| `FBK_ROOT`                | `config.py`       | Explicit package root; overrides module-anchored discovery             |
| `FBK_COOKIES`             | `config.py`       | Explicit cookies.txt path; default `<root>/cookies.txt`                |
| `FBK_IMPERSONATE`         | `config.py`       | curl_cffi TLS/h2 target; default `chrome136`                            |
| `FBK_GOVERNOR`            | `governor.py`     | Master switch: `off`/`0`/`false`/`no` (case-insensitive) disables the pacing gate entirely — tests and dry runs only |
| `FBK_GOVERNOR_<KNOB>`     | `governor.py`     | The eight per-knob overrides: `FBK_GOVERNOR_MIN_GAP`, `FBK_GOVERNOR_MEAN_GAP`, `FBK_GOVERNOR_HOURLY`, `FBK_GOVERNOR_DAILY`, `FBK_GOVERNOR_MUTATION_DAILY`, `FBK_GOVERNOR_COOLDOWN`, `FBK_GOVERNOR_QUIET_HOURS`, `FBK_GOVERNOR_WARMUP`. Malformed values are ignored in favor of the safe default. **Full table: [09-safety-and-opsec.md](09-safety-and-opsec.md).** |
| `FBK_LIVE`                | tests only        | `FBK_LIVE=1` gates the live integration-test suite (needs cookies.txt + network); never read by the CLI runtime |
| `FBK_WS_CAPTURE`          | `realtime/mqtt`   | Test-only override of the MQTT WS capture path used by fixtures        |

There is **no `FBK_DEBUG`**. Debug output is a flag, not an environment
variable: the global `--debug` (see
[03-session-and-auth.md](03-session-and-auth.md) §8).

## 6. Performance note: the lazy curl_cffi import

curl_cffi costs roughly 200 ms to import and is only needed once a transport
is actually constructed. It is imported lazily — inside
`resolve_impersonate()` / `FBTransport.__init__` / the Session facade — never
at module import, so offline commands (`config show`, `cookies inspect`,
`fingerprint`, `doc-ids`, `journal`, `doctor`, `templates`, `governor status`,
…) never pay that import chain at all.

## 7. Packaging

```
pip install -e .        # from the cli/ directory
```

This registers the `fbk` console script (entry point `app:main`, sources map
`src/` → package root, flat layout). From an uninstalled source tree,
`python -m src` is the same entrypoint. The wheel ships `data/**` alongside
the modules (`sources = {"src" = ""}`), so `data/` rides along in
site-packages.

Known limitation (documented in the pyproject comment): with the current
pyproject-parent root derivation, **non-editable installs still need
`FBK_ROOT` set** so discovery can find the shipped `data/` — tracked as the
packaging follow-up.

## Files that feed this guide

- `src/config.py` — `Config.discover`, resolution order, present-or-None profile
- `src/commands/config.py` — `fbk config show`
- `src/commands/fingerprint.py` — `fbk fingerprint show / freeze / clear`
- `src/transport/profile.py` — `ClientProfile`, `validate_coherence`,
  `load_or_default`, `sanitize_user_agent`
- `src/transport/session.py` — `resolve_impersonate` and the fallback chain
- `src/governor.py` — `GovernorConfig.from_env` (FBK_GOVERNOR* parsing)
- `src/constants.py` — `IMPERSONATE_DEFAULT`, `IMPERSONATE_FALLBACKS`
- `pyproject.toml`, `README.md` — packaging, layout, env-var inventory
