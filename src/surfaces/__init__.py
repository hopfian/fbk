"""Surface services: the command-facing API per Facebook surface.

The package barrel re-exports every service class plus the shared
``Surface`` base. Each service encapsulates one docs/02-endpoint-surface-map
family behind typed read/mutation methods; all wire I/O delegates to
``Session``/``GraphQLClient`` (see ``surfaces.base`` for the layering and
governor-transparency invariants), keeping every service offline-testable
against ``tests/fakes.py``.

Public API (docs/02 surface map):
  * FeedService: read, paginate, react, comment, publish, typing
  * MessengerService: threads, send, history, listen (MQTT + DGW)
  * GroupsService: create, join, members, add-members, feed
  * PagesService: create, like, follow, feed
  * FriendsService: list, requests, send/cancel/accept/decline/unfriend
  * SearchService: search
  * MarketplaceService: browse, search, item detail
  * NotificationsService: badge, list, mark-seen
  * ProfileService: me, view
  * SettingsService: show, set-default-privacy, verify-password
  * CommentsService: read, react, edit, delete
  * SavedService: list, save, unsave
  * VideoService: watch feed, badge
  * PhotosService: albums, photos
  * PresenceService: status
  * EventsService, StoriesService, MemoriesService
  * UploadService, VideoUploadService
  * MeasurementService, OverviewService

USER-DOC ANCHOR: cli/docs/08-architecture.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from .base import Surface
from .comments import CommentsService
from .events import EventsService
from .feed import FeedService
from .friends import FriendsService
from .groups import GroupsService
from .marketplace import MarketplaceService
from .measurement import MeasurementService
from .memories import MemoriesService
from .messenger import MessengerService
from .notifications import NotificationsService
from .overview import OverviewService
from .pages import PagesService
from .photos import PhotosService
from .presence import PresenceService
from .profile import ProfileService
from .saved import SavedService
from .search import SearchService
from .settings import SettingsService
from .stories import StoriesService
from .upload import UploadService
from .video import VideoService
from .video_upload import VideoUploadService

__all__ = [
    "CommentsService",
    "EventsService",
    "FeedService",
    "FriendsService",
    "GroupsService",
    "MarketplaceService",
    "MeasurementService",
    "MemoriesService",
    "MessengerService",
    "NotificationsService",
    "OverviewService",
    "PagesService",
    "PhotosService",
    "PresenceService",
    "ProfileService",
    "SavedService",
    "SearchService",
    "SettingsService",
    "StoriesService",
    "Surface",
    "UploadService",
    "VideoService",
    "VideoUploadService",
]
