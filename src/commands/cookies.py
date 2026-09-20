"""Cookie-jar introspection: ``fbk cookies inspect`` — an offline
hygiene audit of the session jar (docs/03 §1.1, docs/03 §9.1,
docs/11 §7).

Loads the jar at the resolved ``cookies_path`` (``FBK_COOKIES`` /
``--cookies`` honored via the shared ``build_config`` seam) and reports
presence plus per-cookie FINGERPRINTS against the cookie taxonomy in
``constants`` — the first operational consumer of the AUTH/DEVICE/INFO
groups, which until now were documentation-only protocol memory
(constants.py calibration notes: the auth pair is validated as a unit
server-side, datr/sb accumulate device reputation, and missing
wd/dpr/presence on an authenticated desktop session is an automation
tell).

Disclosure boundary (docs/12 §4): cookie VALUES never appear in any
output — presence, names, and ``transport.cookies.fingerprint`` prefixes
only. The jar is flat by contract (``load_netscape`` returns
``{name: value}`` and delegates expiry enforcement to the session jar),
so the report notes that expiry is not tracked here rather than
changing the loader's contract.

Exit codes follow the run_command table: a complete auth pair
(c_user + xs) exits 0; a missing pair is a failed precondition —
exit 1 (the jar needs re-export from a logged-in browser, docs/03 §9.1);
a missing/unparseable jar raises the typed CookieLoadError → exit 7.

USER-DOC ANCHOR: cli/docs/03-session-and-auth.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import argparse
import sys
from typing import Any

from constants import AUTH_COOKIES, DEVICE_COOKIES, INFO_COOKIES

from .common import add_common_args, build_config, emit


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Wire the cookies family onto the root parser: inspect."""
    p = sub.add_parser(
        "cookies", help="cookie-jar inspection (offline, never values)")
    csub = p.add_subparsers(dest="cookies_command", required=True)
    insp = csub.add_parser(
        "inspect", help="taxonomy presence + fingerprints; exit 1 when the "
                        "auth pair is missing")
    add_common_args(insp)
    insp.set_defaults(fn=cmd_inspect)


def _taxonomy_rows(jar: dict[str, str], names: tuple[str, ...],
                   fp: Any) -> dict[str, dict[str, Any]]:
    """Presence + fingerprint rows for one taxonomy group — never values.

    Args:
        jar: The flat ``{name: value}`` jar from ``load_netscape``.
        names: The taxonomy's cookie names (constants.AUTH_COOKIES, ...).
        fp: ``transport.cookies.fingerprint`` (passed in so the handler
            imports the transport seam once, at its top).

    Returns:
        ``{name: {"present": bool, "fingerprint": str | None}}`` — the
        fingerprint is the salted SHA-256 prefix (docs/12 §4), emitted
        only for present cookies so absent names carry no fake signal.
    """
    return {
        name: {
            "present": bool(jar.get(name)),
            "fingerprint": fp(jar[name]) if jar.get(name) else None,
        }
        for name in names
    }


def cmd_inspect(args: argparse.Namespace) -> int:
    """Audit the jar offline: taxonomy coverage, fingerprints, hygiene —
    no network, no session (docs/03 §1.1, docs/11 §7).

    Raises:
        CookieLoadError: The jar is missing/unreadable/carries no
            facebook.com rows — propagates to run_command's exit 7.
    """
    from transport.cookies import fingerprint, load_netscape
    cfg = build_config(args)
    jar = load_netscape(cfg.cookies_path)

    covered = set(AUTH_COOKIES) | set(DEVICE_COOKIES) | set(INFO_COOKIES)
    other = sorted(n for n in jar if n not in covered)
    auth_rows = _taxonomy_rows(jar, AUTH_COOKIES, fingerprint)
    device_rows = _taxonomy_rows(jar, DEVICE_COOKIES, fingerprint)
    info_rows = _taxonomy_rows(jar, INFO_COOKIES, fingerprint)
    auth_complete = all(jar.get(name) for name in AUTH_COOKIES)

    payload = {
        "cookies_path": str(cfg.cookies_path),
        "cookie_count": len(jar),
        "auth": auth_rows,
        "device": device_rows,
        "info": info_rows,
        # names outside every taxonomy group — presence implied by listing;
        # fingerprinted under the same never-values boundary
        "other": {name: {"fingerprint": fingerprint(jar[name])}
                  for name in other},
        "auth_pair_complete": auth_complete,
        # load_netscape's contract is a flat {name: value} jar — expiry
        # lives on the Netscape row, not in the loaded dict, and is
        # delegated to the session jar (docs/12 §4.1). Reported as a
        # note instead of widening the loader's contract.
        "note": "flat jar: expiry not tracked (session jar owns expiry policy)",
    }

    def human() -> None:
        print(f"jar   : {cfg.cookies_path} "
              f"({payload['cookie_count']} facebook.com cookies)")
        print(f"auth  : {'complete (c_user + xs)' if auth_complete else 'MISSING pair'}")
        for title, rows in (("auth", auth_rows), ("device", device_rows),
                            ("info", info_rows)):
            for name, row in rows.items():
                if row["present"]:
                    print(f"  [{title:<6}] {name:<10} present  fp={row['fingerprint']}")
                else:
                    print(f"  [{title:<6}] {name:<10} absent")
        others = ", ".join(other) if other else "none"
        print(f"other : {others}")
        print(f"note  : {payload['note']}")

    emit(args, payload, human=human)
    if not auth_complete:
        missing = [name for name in AUTH_COOKIES if not jar.get(name)]
        print(f"error: auth pair incomplete (missing {', '.join(missing)}) — "
              "re-export cookies.txt from a logged-in browser (docs/03 §9.1)",
              file=sys.stderr)
        return 1
    return 0
