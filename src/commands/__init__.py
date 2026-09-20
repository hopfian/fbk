"""Command families: one argparse module per Facebook surface (docs/02).

CONTRACT (assembled by app.build_parser via _COMMAND_MODULES):

  Every family module exposes ``register(sub)``, which attaches its
  (sub)parser tree to the root subparsers action and binds each leaf to
  its handler via ``set_defaults(fn=cmd_<verb>)``. Each ``cmd_<verb>``
  follows one shape: build a Session facade (commands.common.new_session),
  drive exactly one surface-service operation, and emit through
  commands.common.emit. Handlers return the process exit code; typed
  errors propagate up to commands.common.run_command, which owns the
  exit-code contract documented there.

  Cross-module imports stay minimal by design (docs/12 §2 layering:
  nothing outside commands/ imports commands/): shared human-output
  renderers live in commands.render (story_payload, print_stories,
  print_story_page) so every timeline prints identically without
  cross-family private imports.

USER-DOC ANCHOR: cli/docs/08-architecture.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
