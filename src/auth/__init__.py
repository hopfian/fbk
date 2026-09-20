"""Auth package: login-state machine, page bootstrap, token lifecycle,
server-side session teardown (logout.php — docs/02 §2.12).

Public API (docs/03, implementation contract docs/12 3):
  * Bootstrap: the harvested page state (DTSG, LSD, identity, preloads)
  * bootstrap_homepage: fetch + classify + harvest tokens
  * BootstrapPageError: unusable served page (edge soft-block signature)
  * LoginState: the session state machine
  * classify_login_state: page classification
  * extract_*: individual token/preload extractors
  * LogoutService / build_logout_request / jazoest: the logout.php
    POST pair (the harvested logout_hash + fb_dtsg + derived jazoest)

USER-DOC ANCHOR: cli/docs/03-session-and-auth.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from .bootstrap import (
    Bootstrap,
    BootstrapPageError,
    PreloadEntry,
    bootstrap_homepage,
    classify_login_state,
    extract_account_warnings,
    extract_bundles,
    extract_fb_dtsg,
    extract_logout_hash,
    extract_lsd,
    extract_lsd_all,
    extract_preload_registry,
    extract_revision,
    extract_user_id,
)
from .logout import LogoutService, build_logout_request, jazoest
from .state import LoginState

__all__ = [
    "Bootstrap",
    "BootstrapPageError",
    "LoginState",
    "LogoutService",
    "PreloadEntry",
    "bootstrap_homepage",
    "build_logout_request",
    "classify_login_state",
    "extract_account_warnings",
    "extract_bundles",
    "extract_fb_dtsg",
    "extract_logout_hash",
    "extract_lsd",
    "extract_lsd_all",
    "extract_preload_registry",
    "extract_revision",
    "extract_user_id",
    "jazoest",
]
