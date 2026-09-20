"""Photos commands: the own profile's albums and photo grid (docs/02
§2.6 media family) — read-only; the photo fbid these rows carry is the
savable node id `saved save` takes.

USER-DOC ANCHOR: cli/docs/05-reference-content.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from __future__ import annotations

import argparse

from surfaces.photos import PhotosService

from .common import add_common_args, emit, new_session, with_session


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Wire the photos family onto the root parser: albums, photos."""
    parser = sub.add_parser("photos", help="photos surface (own albums + photos)")
    psub = parser.add_subparsers(dest="photos_command", required=True)

    albums = psub.add_parser("albums", help="read the own profile's album tiles")
    add_common_args(albums)
    albums.add_argument(
        "--limit", type=int, default=10, help="maximum albums to emit (default 10)"
    )
    albums.set_defaults(fn=cmd_albums)

    photos = psub.add_parser("photos", help="read the own profile's photo grid")
    add_common_args(photos)
    photos.add_argument("--album-id", default=None,
                        help="numeric album id (from photos albums); scopes "
                             "the grid to that album when given")
    photos.add_argument(
        "--limit", type=int, default=20, help="maximum photos to emit (default 20)"
    )
    photos.set_defaults(fn=cmd_photos)


def cmd_albums(args: argparse.Namespace) -> int:
    """Read the own profile's album tiles (id, name, photo count, url).

    The album ids feed `photos photos --album-id`.

    Returns:
        0 — zero albums is valid account state.
    """
    with with_session(new_session(args)) as session:
        service = PhotosService(session)
        rows = service.albums(limit=args.limit)

        def human() -> None:
            print(f"albums: {len(rows)}")
            for row in rows:
                print(f"{row.get('name') or '?'} {row['id']} "
                      f"{row.get('count') or '?'} {row.get('url') or ''}")

        emit(args, {"albums": rows, "count": len(rows)}, human=human)
        return 0


def cmd_photos(args: argparse.Namespace) -> int:
    """Read the own profile's photo grid, optionally scoped to one album.

    ``--album-id`` takes an id from `photos albums`; absent it reads
    the default grid. Rows carry id, dimensions, and the CDN uri.

    Returns:
        0 — an empty grid is valid account state.
    """
    with with_session(new_session(args)) as session:
        service = PhotosService(session)
        rows = service.photos(args.album_id, limit=args.limit)

        def human() -> None:
            scope = f"album {args.album_id}" if args.album_id else "default grid"
            print(f"photos ({scope}): {len(rows)}")
            for row in rows:
                dims = f"{row.get('width') or '?'}x{row.get('height') or '?'}"
                print(f"{row['id']} {dims} {row.get('uri') or ''}")

        emit(args, {"photos": rows, "count": len(rows)}, human=human)
        return 0
