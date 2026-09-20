"""Client-profile lifecycle: ``fbk fingerprint`` — show / freeze / clear
the transport's coherent TLS/UA identity (docs/08 §7 lifecycle, docs/09 §1
coherence fields).

This family closes the loop ``fbk doctor`` has always warned about: the
doctor's client-profile check advises "freeze a coherent profile once and
replay it forever (docs/08 §7 lifecycle)" — but until now no command could
do the freezing. Fully OFFLINE: no Session, no curl_cffi, no contact with
any facebook.com surface; the whole world is ``ClientProfile``
construction/validation and the ``data/profile.json`` file every live
transport loads through ``load_or_default``.

ARCHITECTURE:

  Identity axes (docs/09 §1 canonical header set):
    The frozen tuple is exactly the header-level identity the edge
    cross-checks: the TLS/h2 impersonation target, the UA string, the
    sec-ch-ua client-hint trio, Accept-Language, IANA timezone, and the
    wd/dpr screen-metric echoes (the docs/08 §5 coherence matrix).
    Rotating any axis between sessions is a known scraper signature —
    the lifecycle is freeze-once-replay-forever.

  Freeze gates on coherence (docs/08 §7 — "the validator is the actual
    value"):
    ``freeze`` refuses to write on ANY coherence problem, exit 1 — an
    incoherent identity must never be persisted. The gate is
    ``ClientProfile.validate_coherence`` ALONE (UA ↔ platform, UA ↔
    sec-ch-ua major, TLS impersonate pin ↔ UA Chrome major — docs/09
    §1.2 — mobile ↔ platform, wd/dpr plausibility). The TLS↔UA axis
    lives in the transport so EVERY profile load is guarded (Session
    construction, doctor, and this command); its deliberate boundary —
    unversioned / non-Chrome / empty pins unguarded, no major to
    compare — is documented on ``validate_coherence``
    (transport/profile.py). No residual command-local check remains:
    the absorbed axis had the identical boundary.

  Sanitized-UA freeze (docs/15 §P9-2):
    A ``--ua`` candidate passes through ``sanitize_user_agent`` BEFORE
    freezing, and the SANITIZED value is what gets persisted: every
    wire consumer (MQTT identity blob, DGW handshake headers) already
    sanitizes at send time and the rewrite is idempotent, so the frozen
    file is byte-what-the-transport-sends — a HeadlessChrome marker
    frozen raw would survive every load round-trip as a loud automation
    fingerprint.

EXIT POLICY: ``show`` and ``clear`` always exit 0 — a coherence
problem or an absent file is DATA reported by an introspection
surface, never a failure (noted in show's help text). Only ``freeze``
can exit 1: refusing to persist an incoherent identity is the house
failed-precondition convention (the run_command contract table in
commands/common.py).

USER-DOC ANCHOR: cli/docs/02-configuration.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from config import Config
from transport.profile import ClientProfile, sanitize_user_agent

from .common import add_common_args, build_config, emit

PROFILE_FILENAME = "profile.json"

# Doctor's own remediation hint (commands/doctor.py) — the lifecycle line
# this family exists to make actionable.
FREEZE_RATIONALE = ("freeze a coherent profile once and replay it forever "
                    "(docs/08 §7 lifecycle)")

# Human-render order == the identity tuple's field order (docs/08 §5);
# notes rides last as the only free-form dict field.
_FIELD_ORDER = ("impersonate", "user_agent", "sec_ch_ua", "sec_ch_ua_platform",
                "sec_ch_ua_mobile", "accept_language", "timezone", "dpr", "wd",
                "notes")
# Label column width: the longest field name (sec_ch_ua_platform) + 1.
_LABEL_W = 18


def _profile_file(cfg: Config) -> Path:
    """The profile.json path regardless of on-disk presence.

    ``Config.discover`` resolves ``profile_path`` only when the file
    exists (present-or-None semantics, config.py); freeze and clear
    need the destination even while absent.

    Args:
        cfg: The resolved runtime config.

    Returns:
        The absolute ``data/profile.json`` path — the very file
        ``load_or_default`` reads on the next live session.
    """
    return (cfg.profile_path if cfg.profile_path is not None
            else cfg.data_dir / PROFILE_FILENAME)


def _render_fields(profile: ClientProfile) -> None:
    """Human-render every ClientProfile field in identity-tuple order.

    Args:
        profile: The profile whose ``model_dump`` is rendered; the
            ``notes`` dict renders as compact JSON.
    """
    data = profile.model_dump()
    for name in _FIELD_ORDER:
        value = data[name]
        rendered = (json.dumps(value, ensure_ascii=False)
                    if isinstance(value, dict) else value)
        print(f"{name:<{_LABEL_W}}: {rendered}")


def cmd_show(args: argparse.Namespace) -> int:
    """Display the RESOLVED profile: the frozen file, or the defaults.

    Loaded-if-present / defaults-if-absent mirrors ``load_or_default``
    exactly; a present-but-broken file is reported as data (exit 0)
    — this is introspection, and a coherence problem is DATA, never a
    failure.

    Returns:
        0 always — see the module EXIT POLICY note.
    """
    cfg = build_config(args)
    path = _profile_file(cfg)
    profile: ClientProfile | None = None
    error: str | None = None
    if cfg.profile_path is not None:
        try:
            profile = ClientProfile.load(cfg.profile_path)
        except (OSError, ValueError, TypeError) as exc:
            error = f"profile.json unreadable ({exc!r})"
    else:
        profile = ClientProfile()
    problems = (profile.validate_coherence()
                if profile is not None else [])
    payload: dict[str, Any] = {
        "source": ("unreadable" if error is not None else
                   "frozen" if cfg.profile_path is not None else "defaults"),
        "path": str(path),
        "profile": profile.model_dump() if profile is not None else None,
        "coherence_problems": problems,
    }
    if error is not None:
        payload["error"] = error

    def human() -> None:
        if error is not None:
            print(f"profile : {path} — UNREADABLE ({error})")
        elif cfg.profile_path is not None:
            print(f"profile : loaded from {cfg.profile_path}")
        else:
            print(f"profile : implicit defaults — not frozen ({path})")
        if profile is not None:
            _render_fields(profile)
        verdict = "PASS" if not problems else "; ".join(problems)
        print(f"{'coherence':<{_LABEL_W}}: {verdict}")

    emit(args, payload, human=human)
    return 0


def cmd_freeze(args: argparse.Namespace) -> int:
    """Validate + persist the identity tuple to ``data/profile.json``.

    Unset flags inherit the ``ClientProfile`` class defaults (the same
    safe tuple ``load_or_default`` falls back to); ``--ua`` is
    sanitized BEFORE construction so the frozen value is
    byte-what-the-transport-sends (docs/15 §P9-2). Refuses to write
    on ANY coherence problem — exit 1 with the problems listed —
    because an incoherent identity must never be persisted
    (docs/08 §7).

    Returns:
        0 when the coherent profile was saved via the transport's own
        ``save()``; 1 when coherence refused the freeze (nothing
        written).
    """
    cfg = build_config(args)
    overrides: dict[str, Any] = {}
    ua_changed = False
    if args.ua:
        sanitized = sanitize_user_agent(args.ua)
        ua_changed = sanitized != args.ua
        overrides["user_agent"] = sanitized
    if args.locale:
        overrides["accept_language"] = args.locale
    if args.timezone:
        overrides["timezone"] = args.timezone
    if args.impersonate:
        overrides["impersonate"] = args.impersonate
    if args.sec_ch_ua:
        overrides["sec_ch_ua"] = args.sec_ch_ua
    if args.sec_ch_ua_platform:
        overrides["sec_ch_ua_platform"] = args.sec_ch_ua_platform
    if args.sec_ch_ua_mobile:
        overrides["sec_ch_ua_mobile"] = args.sec_ch_ua_mobile
    profile = ClientProfile(**overrides)
    problems = profile.validate_coherence()
    path = _profile_file(cfg)
    if not problems:
        profile.save(path)
    payload: dict[str, Any] = {
        "frozen": not problems,
        "path": str(path),
        "profile": profile.model_dump(),
        "problems": problems,
        "ua_sanitized": ua_changed,
    }

    def human() -> None:
        print(f"{'target':<{_LABEL_W}}: {path}")
        _render_fields(profile)
        if ua_changed:
            print(f"{'note':<{_LABEL_W}}: --ua sanitized before freezing "
                  "(HeadlessChrome -> Chrome, docs/15 §P9-2)")
        if problems:
            print(f"{'coherence':<{_LABEL_W}}: REFUSED — "
                  + "; ".join(problems))
        else:
            print(f"{'froze':<{_LABEL_W}}: {path}")
            print(f"{'rationale':<{_LABEL_W}}: {FREEZE_RATIONALE}")

    emit(args, payload, human=human)
    if problems:
        print("error: refusing to freeze an incoherent profile — an "
              "incoherent identity must never be persisted (docs/08 §7)",
              file=sys.stderr)
        return 1
    return 0


def cmd_clear(args: argparse.Namespace) -> int:
    """Remove ``data/profile.json`` — idempotent by design.

    A present file is unlinked; an already-clear state is reported and
    still a success: clearing an unfrozen environment is exactly the
    state the operator asked for.

    Returns:
        0 always — absent-or-removed both leave no frozen profile.
    """
    cfg = build_config(args)
    path = _profile_file(cfg)
    removed = path.is_file()
    if removed:
        path.unlink()
    payload: dict[str, Any] = {"removed": removed, "path": str(path)}

    def human() -> None:
        if removed:
            print(f"removed frozen profile: {path}")
        else:
            print(f"no frozen profile ({path}) — already clear")

    emit(args, payload, human=human)
    return 0


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Wire the fingerprint family onto the root parser: show/freeze/clear."""
    p = sub.add_parser(
        "fingerprint",
        help="manage the transport's TLS/UA coherence profile "
             "(show / freeze / clear; fully offline)")
    csub = p.add_subparsers(dest="fingerprint_command", required=True)
    show = csub.add_parser(
        "show", help="resolved profile — frozen file or transport defaults, "
                     "every field + coherence verdict (always exit 0: a "
                     "coherence problem is data, not failure)")
    add_common_args(show)
    show.set_defaults(fn=cmd_show)
    freeze = csub.add_parser(
        "freeze", help="validate + persist the profile to data/profile.json; "
                       "exit 1, nothing written, on any coherence problem")
    add_common_args(freeze)
    freeze.add_argument("--ua", default=None, metavar="UA",
                        help="user agent; sanitized (HeadlessChrome -> "
                             "Chrome, docs/15 §P9-2) before freezing")
    freeze.add_argument("--locale", default=None, metavar="LOC",
                        help="Accept-Language value (e.g. en-US,en;q=0.9)")
    freeze.add_argument("--tz", default=None, dest="timezone", metavar="TZ",
                        help="IANA timezone (e.g. America/New_York)")
    freeze.add_argument("--impersonate", default=None, metavar="TARGET",
                        help="curl_cffi TLS/h2 target; must agree with the "
                             "UA's Chrome major (docs/16 §6 pinning policy)")
    freeze.add_argument("--sec-ch-ua", default=None, dest="sec_ch_ua",
                        metavar="HINT",
                        help='sec-ch-ua client hint (e.g. \'"Chromium";v="136", '
                             "...'); its Chrome major must agree with --ua "
                             "(docs/08 §5)")
    freeze.add_argument("--sec-ch-ua-platform", default=None,
                        dest="sec_ch_ua_platform", metavar="PLATFORM",
                        help='sec-ch-ua-platform (e.g. "Windows"); must '
                             "match the UA's OS class (docs/08 §5)")
    freeze.add_argument("--sec-ch-ua-mobile", default=None,
                        dest="sec_ch_ua_mobile", metavar="FLAG",
                        help="sec-ch-ua-mobile (?0 desktop / ?1 mobile); "
                             "cross-checked against platform and wd")
    freeze.set_defaults(fn=cmd_freeze)
    clear = csub.add_parser(
        "clear", help="remove data/profile.json (already-clear is success)")
    add_common_args(clear)
    clear.set_defaults(fn=cmd_clear)
