"""Environment self-diagnostic: ``fbk doctor`` — the one-shot, fully
OFFLINE answer to "is my environment whole?" before a live session.

``config show`` reports resolved paths and ``cookies inspect`` audits
the jar; doctor composes the whole pre-flight after a credential scrub
+ re-auth cycle: registry tiers (v3 + the v2 fallback), the capture
assets, client-profile coherence, the impersonation target, the cookie
jar, state-dir writability, the journal census, and the self-healing
visibility readout (the ambient FBK_HEAL switch + the healing.jsonl
census) — each a pass/warn/fail line with a remediation hint whenever
degraded, and ``--json`` for the structured list.

OFFLINE / NON-MUTATION CONTRACT:
  * no Session, no journal records, no governor ticks, no contact with
    any facebook.com surface;
  * the only filesystem write is the state-dir probe
    (``state/.doctor-probe``, removed immediately by the same probe);
  * the impersonate check reuses ``transport.session.resolve_impersonate``,
    whose validation probe opens a connection to 127.0.0.1:9 — a local
    refused-port probe distinguishing unsupported targets from valid
    ones (docs/12 §3) — never external traffic.

EXIT POLICY: 0 while every CRITICAL check passes — WARNs allowed (an
absent jar is the expected clean post-scrub state, so doctor must pass
on a healthy offline environment); 1 as soon as any critical check
fails, the house failed-precondition convention (the run_command
contract table in commands/common.py).

USER-DOC ANCHOR: cli/docs/07-reference-tooling.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import argparse
import json
import tomllib
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Any

from config import Config
from constants import AUTH_COOKIES
from graphql.errors import RegistryLoadError, RegistryMissError
from graphql.registry import DocIdRegistry
from healing import (
    KIND_DOC_ID_RETRY,
    KIND_DOCTOR_FIX,
    KIND_GOVERNOR_STATE_REBUILD,
    KIND_REGISTRY_REFRESH,
    KIND_TOKEN_CACHE_REBUILD,
    KIND_TRANSPORT_RETRY,
    HealingLog,
    healing_enabled,
)
from journal.recorder import iter_journals
from transport.cookies import CookieLoadError, load_netscape
from transport.profile import ClientProfile
from transport.session import FingerprintRejectedError, resolve_impersonate

from .common import add_common_args, build_config, emit

PASS = "pass"
WARN = "warn"
FAIL = "fail"

# The capture assets the surface code actually loads (surfaces/base.py
# load_template call sites in feed/comments/settings/upload). Doctor proves
# parseability only — the sibling `templates` command owns the DEEP
# introspection.
_CAPTURE_ASSETS = ("captured_mutations.json", "captured_comment_mutations.json",
                   "captured_composer.json")

# Registry tiers, freshest first (graphql/registry.py _PRIORITY semantics).
_REGISTRY_V3 = "doc_id_registry_v3.json"
_REGISTRY_V2 = "doc_id_registry_v2.json"

_STATE_PROBE_NAME = ".doctor-probe"
_GOVERNOR_STATE = "governor_state.json"

# The self-healing readout: the 24h window plus the closed KIND_* vocabulary
# in healing.py's declaration order (by_kind keys are log-consumer-stable).
_HEALING_LOG = "healing.jsonl"
_HEALING_WINDOW_S = 86400.0
_HEALING_KINDS = (KIND_REGISTRY_REFRESH, KIND_DOC_ID_RETRY,
                  KIND_TOKEN_CACHE_REBUILD, KIND_TRANSPORT_RETRY,
                  KIND_GOVERNOR_STATE_REBUILD, KIND_DOCTOR_FIX)


@dataclass
class Check:
    """One doctor finding: a status line plus a remediation hint.

    Attributes:
        name: Short check label, aligned in the human output.
        status: ``"pass"`` | ``"warn"`` | ``"fail"``.
        critical: Whether a ``fail`` here fails the whole run (exit 1);
            warns never do, critical or not.
        detail: The finding itself (paths, counts, typed-error text).
        hint: Remediation guidance, emitted whenever status is not
            ``pass``; ``None`` on a clean pass.
    """

    name: str
    status: str
    critical: bool
    detail: str
    hint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """The ``--json`` element shape (pinned by tests/unit/test_doctor.py).

        Returns:
            The check as a plain JSON-ready dict: ``name``, ``status``,
            ``critical``, ``detail``, ``hint``.
        """
        return {"name": self.name, "status": self.status,
                "critical": self.critical, "detail": self.detail,
                "hint": self.hint}


def _registry_tier(data_dir: Path, name: str) -> tuple[DocIdRegistry | None, str]:
    """Probe one registry tier through ``DocIdRegistry.from_file``.

    Args:
        data_dir: Directory holding the ``doc_id_registry_*.json`` files.
        name: Registry file name (``doc_id_registry_v3.json``, ...).

    Returns:
        ``(registry, "")`` on success, or ``(None, problem)`` where
        ``problem`` is the typed RegistryLoadError/RegistryMissError
        message — it names the file and the recovery action (docs/13 §2
        covers the re-harvest recovery procedure).
    """
    try:
        return DocIdRegistry.from_file(data_dir, name), ""
    except (RegistryLoadError, RegistryMissError) as exc:
        return None, str(exc)


def _check_registry(cfg: Config) -> Check:
    """Registry tiers: v3 present + parses, v2 as the fallback tier.

    v3 (the refresh output) failing is CRITICAL — no GraphQL command can
    resolve doc_ids; a v2-only degradation keeps reads working through
    v3 (from_assets fail-soft) and is a WARN (docs/13 §2 recovery).
    """
    v3, v3_problem = _registry_tier(cfg.data_dir, _REGISTRY_V3)
    v2, v2_problem = _registry_tier(cfg.data_dir, _REGISTRY_V2)
    if v3 is None:
        return Check("registry", FAIL, True, v3_problem,
                     "registry recovery (docs/13 §2): re-harvest with "
                     "scripts/02_harvest_bundles.py or rewrite v3 via "
                     "`fbk registry refresh --save`")
    if v2 is None:
        return Check("registry", WARN, True,
                     f"{_REGISTRY_V3} {len(v3)} pairs "
                     f"(revision {v3.revision or 'unknown'}); "
                     f"v2 fallback tier unusable: {v2_problem}",
                     "reads still serve from v3 (fail-soft, graphql/registry.py); "
                     "restore v2 to regain the fallback tier")
    return Check("registry", PASS, True,
                 f"{_REGISTRY_V3} {len(v3)} pairs "
                 f"(revision {v3.revision or 'unknown'}); "
                 f"v2 fallback {len(v2)} pairs")


def _check_captures(cfg: Config) -> Check:
    """Light census: every code-referenced capture asset parses as JSON
    with a non-empty ``mutations`` list (docs/15 §P2-3 verbatim-replay
    invariant — the mutation surfaces break without these assets)."""
    problems: list[str] = []
    total = 0
    for name in _CAPTURE_ASSETS:
        path = cfg.data_dir / name
        if not path.is_file():
            problems.append(f"{name}: missing")
            continue
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            mutations = doc["mutations"]
        except (json.JSONDecodeError, KeyError, TypeError, OSError) as exc:
            problems.append(f"{name}: corrupt ({exc!r})")
            continue
        if not isinstance(mutations, list) or not mutations:
            problems.append(f"{name}: empty mutations list")
            continue
        total += len(mutations)
    if problems:
        return Check("capture assets", FAIL, True, "; ".join(problems),
                     "restore the capture asset from the repo or re-capture it "
                     "(docs/15 §P2-3/P3-3)")
    return Check("capture assets", PASS, True,
                 f"{len(_CAPTURE_ASSETS)}/{len(_CAPTURE_ASSETS)} referenced "
                 f"assets parse ({total} captured mutations)")


def _check_profile(cfg: Config) -> tuple[Check, ClientProfile]:
    """profile.json presence + coherence (docs/08 §5 identity tuple).

    Absent is a WARN, not a failure: the transport's built-in default
    profile is coherence-safe by construction (advisory only). A file
    that IS present but unreadable or incoherent is CRITICAL — it must
    never reach the wire.

    Returns:
        The finding plus the RESOLVED profile the impersonate check
        reuses: the loaded one, or the safe default when absent.
    """
    if cfg.profile_path is None:
        profile = ClientProfile()
        return Check(
            "client profile", WARN, True,
            f"no profile.json — transport defaults in use "
            f"({profile.impersonate} / {profile.accept_language} / "
            f"{profile.timezone}); advisory, not a failure",
            "freeze a coherent profile once and replay it forever "
            "(docs/08 §7 lifecycle)"), profile
    try:
        profile = ClientProfile.load(cfg.profile_path)
    except (OSError, ValueError, TypeError) as exc:
        return Check("client profile", FAIL, True,
                     f"profile.json unreadable ({exc!r})",
                     "repair or delete profile.json — a broken profile must "
                     "never reach the wire (docs/08 §7)"), ClientProfile()
    problems = profile.validate_coherence()
    if problems:
        return Check("client profile", FAIL, True, "; ".join(problems),
                     "fix the contradicting axes (docs/08 §5 coherence "
                     "matrix) before any live session"), profile
    return Check("client profile", PASS, True,
                 f"coherent: {profile.impersonate} / {profile.accept_language} "
                 f"/ {profile.timezone}"), profile


def _check_impersonate(cfg: Config, profile: ClientProfile) -> Check:
    """Resolve the wire impersonation target via the local refused-port probe.

    ``Session`` freezes ``config.impersonate`` onto the transport
    (session.py), so THAT value is probed; the resolved target is then
    compared against the profile's own pin for identity-tuple
    coherence (docs/16 §6 pinning policy).
    """
    try:
        resolved = resolve_impersonate(cfg.impersonate)
    except FingerprintRejectedError as exc:
        return Check("impersonate", FAIL, True, str(exc),
                     "upgrade curl_cffi to a build supporting the pinned "
                     "targets (docs/12 §3 probe methodology)")
    detail = (f"resolved {resolved} via the local refused-port probe "
              "(127.0.0.1:9, docs/12 §3 — offline, no external traffic)")
    if resolved != profile.impersonate:
        return Check("impersonate", WARN, True,
                     f"{detail}; profile pins {profile.impersonate} — the "
                     "identity tuple diverges",
                     "align FBK_IMPERSONATE with the profile's pinned target "
                     "(docs/16 §6)")
    return Check("impersonate", PASS, True, detail)


def _check_cookies(cfg: Config) -> Check:
    """Jar presence + auth-pair completeness (c_user + xs, the
    constants taxonomy) — advisory after a scrub: an absent or partial
    jar is the EXPECTED clean offline state, never a failure."""
    if not cfg.cookies_path.is_file():
        return Check("cookie jar", WARN, False,
                     f"absent ({cfg.cookies_path}) — live commands will exit 7 "
                     "until re-auth",
                     "re-export cookies.txt from a logged-in browser "
                     "(docs/03 §9.1)")
    try:
        jar = load_netscape(cfg.cookies_path)
    except CookieLoadError as exc:
        return Check("cookie jar", WARN, False, str(exc),
                     "live commands will exit 7 until the jar is re-exported "
                     "from a logged-in browser (docs/03 §9.1)")
    missing = [name for name in AUTH_COOKIES if not jar.get(name)]
    if missing:
        return Check("cookie jar", WARN, False,
                     f"{len(jar)} cookies, auth pair incomplete (missing "
                     f"{', '.join(missing)})",
                     "re-export from a logged-in browser — the pair is "
                     "validated as a unit server-side (constants taxonomy)")
    return Check("cookie jar", PASS, False,
                 f"{len(jar)} cookies, auth pair complete (c_user + xs)")


def _probe_state_writable(state_dir: Path) -> str | None:
    """Probe state-dir writability: write + immediately remove the probe file.

    The probe file (``state/.doctor-probe``) is the ONLY write doctor
    ever performs, and it is unlinked in the same probe. Unit-tested via
    a mocked ``Path.write_text`` raising OSError — real read-only
    filesystems are not portably testable across OSes.

    Args:
        state_dir: The ``state/`` directory under the package root.

    Returns:
        ``None`` when the directory is writable; otherwise a
        description of the OSError the probe raised.
    """
    probe = state_dir / _STATE_PROBE_NAME
    try:
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return str(exc)
    return None


def _check_state(cfg: Config) -> Check:
    """state/ exists + is writable; the governor state file parses when
    present.

    A corrupt ``governor_state.json`` is a WARN, never a fail: the load
    semantics are fail-soft BY DESIGN (governor.py — caps and cooldowns
    reset to defaults on the next launch, and the next successful
    persist repairs the file).
    """
    if not cfg.state_dir.is_dir():
        return Check("state dir", FAIL, True, f"missing ({cfg.state_dir})",
                     f"create it: mkdir \"{cfg.state_dir}\" — journals, the "
                     "governor state, and the token cache persist there")
    problem = _probe_state_writable(cfg.state_dir)
    if problem is not None:
        return Check("state dir", FAIL, True, f"not writable: {problem}",
                     "fix the directory's permissions — journals, the governor "
                     "state, and the token cache persist there")
    gov = cfg.state_dir / _GOVERNOR_STATE
    if not gov.is_file():
        return Check("state dir", PASS, True,
                     f"writable; no governor state yet ({cfg.state_dir})")
    raw: Any = None
    try:
        raw = json.loads(gov.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return Check("state dir", WARN, True,
                     f"writable, but {_GOVERNOR_STATE} is corrupt ({exc!r}) — "
                     "caps/cooldowns reset to defaults on the next launch "
                     "(fail-soft, governor.py)",
                     "no action needed: the next successful governor persist "
                     "repairs the file")
    if not isinstance(raw, dict):
        return Check("state dir", WARN, True,
                     f"writable, but {_GOVERNOR_STATE} is corrupt "
                     f"(expected a JSON object, got {type(raw).__name__}) — "
                     "caps/cooldowns reset to defaults on the next launch "
                     "(fail-soft, governor.py)",
                     "no action needed: the next successful governor persist "
                     "repairs the file")
    return Check("state dir", PASS, True,
                 f"writable; governor state parses "
                 f"(day_count={raw.get('day_count')}, "
                 f"cooldown_until={raw.get('cooldown_until')})")


def _check_journals(cfg: Config) -> Check:
    """Informational: enumerate state/ journal files (count only) via the
    journal module's own vocabulary (``iter_journals`` — never a duplicate
    glob)."""
    count = len(list(iter_journals(cfg.state_dir)))
    return Check("journal dir", PASS, False,
                 f"{count} journal file(s) under {cfg.state_dir}")


def _check_self_healing(cfg: Config) -> tuple[Check, dict[str, Any]]:
    """Self-healing visibility: the FBK_HEAL switch + the healing.jsonl census.

    Read-only over ``<journal_dir>/healing.jsonl`` (the path Session wires
    the HealingContext to) through the healing module's own API —
    ``count_since`` for the 24h census, ``last_event`` for the newest row
    — honoring the AMBIENT ``FBK_HEAL`` switch (no new flags, no
    Session). Advisory by construction: FAIL is unreachable (healing
    problems must not fail the doctor), and a disabled switch is a
    legitimate operator choice, still a pass. The single WARN is a
    non-empty log file that yields zero parseable rows — a torn/corrupt
    log loses the self-repair audit trail (visibility, not function:
    healing reads are fail-soft and the next append starts a fresh
    readable log).

    Returns:
        The finding plus the structured readout carried by the ``--json``
        payload's top-level ``self_healing`` field: ``enabled``, the log
        path, the 24h event count, the per-kind breakdown over the closed
        KIND_* vocabulary, and the newest event (``None`` when the log is
        absent or unreadable).
    """
    path = cfg.journal_dir / _HEALING_LOG
    log = HealingLog(path)
    enabled = healing_enabled()
    events = log.count_since(_HEALING_WINDOW_S)
    by_kind = {kind: log.count_since(_HEALING_WINDOW_S, kind)
               for kind in _HEALING_KINDS}
    last = log.last_event()
    try:
        nonempty = path.is_file() and path.stat().st_size > 0
    except OSError:
        nonempty = False
    readout: dict[str, Any] = {"enabled": enabled, "log": str(path),
                               "events_24h": events, "by_kind": by_kind,
                               "last": last}
    if nonempty and last is None:
        return Check(
            "self-healing", WARN, False,
            f"healing log present but unreadable ({path}) — 0 parseable rows; "
            "self-repair visibility lost (fail-soft: healing itself is "
            "unaffected)",
            "no action needed — the next healing append starts a fresh "
            "readable log; delete the file to discard the torn history",
        ), readout
    switch = "on" if enabled else "off (FBK_HEAL)"
    if not path.is_file():
        return Check("self-healing", PASS, False,
                     f"healing {switch}; no log yet ({path})"), readout
    tail = f"; last: {last['kind']}: {last['trigger']}" if last else ""
    return Check("self-healing", PASS, False,
                 f"healing {switch}; {events} event(s) in 24h ({path})"
                 + tail), readout


def _check_version(cfg: Config) -> Check:
    """Informational: version provenance consistency.

    ``app.py`` resolves the version from the installed dist metadata
    (importlib), falling back to a source-tree literal when uninstalled;
    doctor imports that resolved value lazily (single source of truth —
    no literal duplicated into this module) and compares it against the
    version DECLARED in the tree's pyproject.toml. Wheels do not ship
    pyproject.toml (docs/12 §8 self-containment), so where the file is
    absent the comparison is skipped, never failed.
    """
    from app import _VERSION  # lazy: app owns the literal (no drift here)
    pyproject = cfg.root / "pyproject.toml"
    if not pyproject.is_file():
        try:
            installed = _pkg_version("fbk")
            origin = "installed dist metadata"
        except PackageNotFoundError:
            installed = None
            origin = "source-tree fallback literal"
        shown = installed if installed is not None else _VERSION
        return Check("version", PASS, False,
                     f"{shown} ({origin}; no pyproject.toml under the root — "
                     "declared-version comparison skipped)")
    try:
        declared = (tomllib.loads(pyproject.read_text(encoding="utf-8"))
                    ["project"]["version"])
    except (tomllib.TOMLDecodeError, KeyError, TypeError, OSError):
        return Check("version", PASS, False,
                     f"{_VERSION} (pyproject.toml unreadable under the root — "
                     "declared-version comparison skipped)")
    if declared == _VERSION:
        return Check("version", PASS, False,
                     f"{_VERSION} (resolved version matches the pyproject "
                     "declaration)")
    return Check("version", PASS, False,
                 f"{_VERSION} (resolved) vs {declared} (pyproject declared) — "
                 "informational; reinstall to sync the two")


def _quarantine(path: Path) -> Path | None:
    """Rename one corrupt state file aside (``<name>.corrupt-<epoch>``).

    The doctor --fix repair primitive: the damaged file is MOVED, never
    deleted — the operator keeps the evidence, the live path becomes
    absent (every loader's fail-soft regeneration then owns recovery).
    Returns the quarantine path, or None when the rename failed (the
    fix pass is best-effort and never breaks the doctor run).
    """
    import time as _time
    target = path.with_name(f"{path.name}.corrupt-{int(_time.time())}")
    try:
        path.rename(target)
    except OSError:
        return None
    return target


def _json_parseable(path: Path) -> bool:
    """Whether the file exists and parses as JSON (the fix precondition)."""
    if not path.is_file():
        return True  # absent = nothing to fix
    try:
        json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return False
    return True


def _apply_fixes(cfg: Config) -> list[dict[str, str]]:
    """Quarantine every corrupt offline-state file the doctor can detect.

    The ``--fix`` pass: governor_state.json, token_cache.json, and a
    fully-unparseable healing.jsonl are the state files whose corruption
    the checks WARN/FAIL on. Each is quarantined (renamed aside, never
    deleted) and the action is recorded in the healing log
    (``doctor-fix`` kind) so the audit trail shows WHY the file
    vanished. Parseable files are left untouched; nothing here touches
    data/ (the registry heal is network-side and stays governed).

    Note:
        The healing log is JSONL — many JSON documents, not one — so its
        corruption test is ROW-based (the same WARN condition the
        self-healing check uses: non-empty file, zero parseable rows),
        not a whole-file ``json.loads``.
    """
    state = cfg.journal_dir
    candidates = {
        "governor_state.json": KIND_GOVERNOR_STATE_REBUILD,
        "token_cache.json": KIND_TOKEN_CACHE_REBUILD,
        _HEALING_LOG: KIND_DOCTOR_FIX,
    }
    log = HealingLog(state / _HEALING_LOG)
    fixed: list[dict[str, str]] = []
    for name, kind in candidates.items():
        path = state / name
        if name == _HEALING_LOG:
            corrupt = (path.is_file() and path.stat().st_size > 0
                       and HealingLog(path).last_event() is None)
        else:
            corrupt = path.is_file() and not _json_parseable(path)
        if corrupt:
            # the healing log is special: quarantining it removes the
            # audit trail, so the event must be recorded INTO THE FRESH
            # file (append creates it lazily)
            target = _quarantine(path)
            if target is None:
                continue
            log.append(kind, f"quarantined corrupt {name}",
                       f"renamed to {target.name} (fbk doctor --fix)")
            fixed.append({"file": name, "quarantined_to": target.name})
    return fixed


def cmd_doctor(args: argparse.Namespace) -> int:
    """Run every offline check; report pass/warn/fail + remediation hints.

    Returns:
        0 while every CRITICAL check passes (warns allowed — an absent
        jar is the expected post-scrub state), 1 as soon as any critical
        check fails (the house failed-precondition convention).
    """
    cfg = build_config(args)
    profile_check, profile = _check_profile(cfg)
    healing_check, healing_readout = _check_self_healing(cfg)
    checks = [
        _check_registry(cfg),
        _check_captures(cfg),
        profile_check,
        _check_impersonate(cfg, profile),
        _check_cookies(cfg),
        _check_state(cfg),
        _check_journals(cfg),
        healing_check,
        _check_version(cfg),
    ]
    ok = all(c.status != FAIL for c in checks if c.critical)
    payload: dict[str, Any] = {"ok": ok,
                               "checks": [c.to_dict() for c in checks],
                               "self_healing": healing_readout}

    # the --fix pass (offline self-repair): after reporting what it FOUND,
    # quarantine every corrupt state file so the next run starts clean.
    # Never deletes — renames aside, and every quarantine is logged.
    fixed: list[dict[str, str]] | None = None
    if getattr(args, "fix", False):
        fixed = _apply_fixes(cfg)
        if fixed:
            payload["fixed"] = fixed

    def human() -> None:
        for c in checks:
            print(f"[{c.status.upper():<4}] {c.name:<15} {c.detail}")
            if c.status != PASS and c.hint:
                print(f"       hint: {c.hint}")
        if fixed:
            for entry in fixed:
                print(f"[FIXED] {entry['file']:<15} quarantined -> "
                      f"{entry['quarantined_to']}")
        passed = sum(1 for c in checks if c.status == PASS)
        warned = sum(1 for c in checks if c.status == WARN)
        failed = sum(1 for c in checks if c.status == FAIL)
        verdict = "environment healthy" if ok else "environment broken"
        print(f"doctor: {len(checks)} checks — {passed} passed, {warned} "
              f"warned, {failed} failed — {verdict}")

    emit(args, payload, human=human)
    return 0 if ok else 1


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Wire the doctor command onto the root parser (leaf — no children)."""
    p = sub.add_parser(
        "doctor",
        help="one-shot offline environment self-diagnostic: registry, "
             "assets, profile, impersonate, jar, state (pass/warn/fail)")
    add_common_args(p)
    p.add_argument(
        "--fix", action="store_true",
        help="after diagnosing, quarantine corrupt offline-state files "
             "(governor state, token cache, torn healing log) — renamed "
             "aside, never deleted; every quarantine is logged")
    p.set_defaults(fn=cmd_doctor)
