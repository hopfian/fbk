"""Search commands: top-search replay with typed results (docs/15 §P3-5;
docs/02 §2.4 search family).

Drives SearchService via the preload-replay strategy: the server's own
SSR variables are replayed verbatim — the most faithful and
bypass-resistant read available. Search is the most integrity-gated
read surface (docs/02 §2.4, docs/10 §2): expect checkpoint-class
enforcement here earlier than on profile reads, especially for
anonymous datr-keyed actors.

USER-DOC ANCHOR: cli/docs/05-reference-content.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import argparse

from domain.common import SearchResponse
from surfaces.search import SearchService

from .common import add_common_args, emit, new_session, with_session


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Wire the single `search` command onto the root parser."""
    p = sub.add_parser("search", help="search Facebook top results (preload replay)")
    add_common_args(p)
    p.add_argument("query", nargs="+", help="the search terms (naturally quoted)")
    p.add_argument("--limit", type=int, default=20,
                   help="maximum typed results to emit (default 20)")
    p.set_defaults(fn=cmd_search)


def _print_response(resp: SearchResponse) -> None:
    """Human output: the query, result count, raw payload size, then one
    typed line per result ([__typename] name url)."""
    print(f"query   : {resp.query}")
    print(f"results : {len(resp.results)} (raw payload {resp.raw_size} bytes)")
    for r in resp.results:
        print(f"[{r.typename}] {r.name or '?'} {r.url or ''}")


def cmd_search(args: argparse.Namespace) -> int:
    """Run one top-search replay and emit typed results.

    The free-text query joins the argv terms (naturally quoted);
    ``--limit`` caps typed result rows — the raw payload size reports
    regardless, so degraded-but-large responses stay observable
    (docs/10 §3.5).

    Returns:
        0 — zero results is a valid observation (and, on a
        soft-throttled session, itself diagnostic).
    """
    with with_session(new_session(args)) as session:
        service = SearchService(session)
        resp = service.search(" ".join(args.query), limit=args.limit)
        emit(args, resp.model_dump(),
             human=lambda: _print_response(resp))
        return 0
