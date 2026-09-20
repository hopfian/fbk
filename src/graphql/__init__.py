"""GraphQL package: persisted-query client, registry, wire parsing.

Public API (docs/04):
  * GraphQLClient: the /api/graphql/ transport (auto token refresh)
  * DocIdRegistry: the friendly_name -> doc_id lookup table
  * Typed errors: NotLoggedInError, CheckpointError, RateLimitedError,
    DocIdStaleError, GraphQLProtocolError, RegistryMissError,
    RegistryLoadError
  * Parsing: parse_incremental, merge_docs, strip_legacy_prefix
  * Registry refresh: refresh_registry, harvest_pairs, RegistryDiff

USER-DOC ANCHOR: cli/docs/08-architecture.md — ship a matching edit to
that guide in the same change whenever this module's behavior changes.
"""
from .client import GraphQLClient
from .errors import (
    CheckpointError,
    DocIdStaleError,
    FBGraphError,
    GraphQLProtocolError,
    NotLoggedInError,
    RateLimitedError,
    RegistryLoadError,
    RegistryMissError,
)
from .parsing import merge_docs, parse_incremental, strip_legacy_prefix
from .registry import DocIdRegistry
from .registry_refresh import (
    RegistryDiff,
    RegistryRefreshError,
    harvest_pairs,
    refresh_registry,
)

__all__ = [
    "CheckpointError",
    "DocIdRegistry",
    "DocIdStaleError",
    "FBGraphError",
    "GraphQLClient",
    "GraphQLProtocolError",
    "NotLoggedInError",
    "RateLimitedError",
    "RegistryDiff",
    "RegistryLoadError",
    "RegistryMissError",
    "RegistryRefreshError",
    "harvest_pairs",
    "merge_docs",
    "parse_incremental",
    "refresh_registry",
    "strip_legacy_prefix",
]
