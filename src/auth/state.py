"""Session-state machine for the auth package (docs/03 §3, docs/12 §3).

:class:`LoginState` enumerates every classification a served page can
land in. Per the docs/12 §3 implementation contract, health is
re-evaluated on every response — no code path may assume state, it must
query the classifier; ``CHECKPOINT`` and ``LOGGED_OUT`` are sticky with
a cooldown at the Session layer, and the soft-block heuristic annotates
  the journal without blocking requests.

USER-DOC ANCHOR: cli/docs/03-session-and-auth.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import enum


class LoginState(enum.Enum):
    """Every state an authenticated bootstrap can land in.

    Produced by ``auth.bootstrap.classify_login_state`` from one served
    page (docs/03 §3): the state is evidence-driven — markers, the
    ``is_checkpointed`` flag, and the ``c_user`` coherence check — never
    assumed from prior responses.
    """

    UNKNOWN = "unknown"
    LOGGED_IN = "logged_in"          # c_user/xs accepted, DTSG harvested
    LOGGED_OUT = "logged_out"        # login form served / uid mismatch
    CHECKPOINT = "checkpoint"       # integrity challenge (is_checkpointed:true)
    SHADOW_SUSPECT = "shadow_suspect"  # 200-but-degraded heuristic (docs/10 §3)
