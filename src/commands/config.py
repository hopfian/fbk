"""Config introspection: ``fbk config show`` — the resolved runtime
environment, fully OFFLINE (docs/12 §4 path resolution).

Answers "what config am I running" after a scrub or re-auth without a
single wire call: every resolved Config field (root/data/state paths,
the secret-bearing inputs' on-disk existence, the pinned impersonation
target, timeout, endpoint), plus the pacing engine's caps — reused
verbatim from ``default_governor().status()`` (the governor module owns
that vocabulary; this surface never duplicates it). No network, no
cookies.txt required: a missing jar is a REPORTED fact, not a failure.

Resolution honors ``FBK_ROOT`` / ``FBK_COOKIES`` / ``FBK_IMPERSONATE``
and the ``--root`` / ``--cookies`` flags through the shared
``build_config`` seam — the same discovery every live command uses, so
what this command prints is exactly what the next run will act on.

USER-DOC ANCHOR: cli/docs/02-configuration.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import argparse

from .common import add_common_args, build_config, emit


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Wire the config family onto the root parser: show."""
    p = sub.add_parser(
        "config", help="resolved runtime configuration (offline)")
    csub = p.add_subparsers(dest="config_command", required=True)
    show = csub.add_parser(
        "show", help="resolved paths, identity, wire shape + governor caps")
    add_common_args(show)
    show.set_defaults(fn=cmd_show)


def cmd_show(args: argparse.Namespace) -> int:
    """Print the resolved Config plus the governor's caps — no session,
    no network, no cookie jar required (docs/12 §4).

    Path fields render with their on-disk existence for the two
    secret-bearing inputs (cookies.txt, profile.json) so the operator
    can audit environment sanity after a scrub/re-auth without a live
    call. The governor block is ``default_governor().status()`` as-is:
    the caps/counter vocabulary lives in one place (governor.py), and
    this surface only embeds it.

    Returns:
        0 — a missing cookies.txt or profile.json is a reported fact
        ("cookies_present": false), never an error: this command exists
        precisely to inspect the environment when inputs are absent.
    """
    from governor import default_governor
    cfg = build_config(args)
    g = default_governor().status()
    payload = {
        "root": str(cfg.root),
        "data_dir": str(cfg.data_dir),
        "state_dir": str(cfg.state_dir),
        "cookies_path": str(cfg.cookies_path),
        "cookies_present": cfg.cookies_path.is_file(),
        # discover() sets profile_path only when the file exists on disk
        # (config.py), so presence is inherent in the None-ness here.
        "profile_path": str(cfg.profile_path) if cfg.profile_path else None,
        "profile_present": cfg.profile_path is not None,
        "impersonate": cfg.impersonate,
        "timeout": cfg.timeout,
        "graphql_endpoint": cfg.graphql_endpoint,
        "governor": g,
    }

    def human() -> None:
        print(f"root             : {cfg.root}")
        print(f"data dir         : {cfg.data_dir}")
        print(f"state dir        : {cfg.state_dir}")
        cookies_state = "present" if cfg.cookies_path.is_file() else "MISSING"
        print(f"cookies          : {cfg.cookies_path} ({cookies_state})")
        profile = (f"{cfg.profile_path} (present)"
                   if cfg.profile_path else "none (transport default)")
        print(f"profile          : {profile}")
        print(f"impersonate      : {cfg.impersonate}")
        print(f"timeout          : {cfg.timeout}s")
        print(f"graphql endpoint : {cfg.graphql_endpoint}")
        print(f"governor         : {'enabled' if g['enabled'] else 'disabled'} — "
              f"caps {g['hourly_cap']}/hour, {g['daily_cap']}/day, "
              f"{g['mutation_daily_cap']} mutations/day")

    emit(args, payload, human=human)
    return 0
