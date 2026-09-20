"""``python -m src`` shim — delegates to app.main, the same entrypoint the
installed ``fbk`` console script binds; kept so the CLI runs from an
uninstalled source tree without ``pip install -e``.

USER-DOC ANCHOR: cli/docs/01-getting-started.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from app import main

if __name__ == "__main__":
    raise SystemExit(main())
