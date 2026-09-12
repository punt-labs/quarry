"""Quarry's daemon wire contract — Pydantic request/response/error models.

One request/response model per daemon REST operation under the versioned prefix.
This package is the single source of truth for the wire shape: the daemon imports
it to document its handlers, and ``QuarryClient`` will import the same models to
build requests and parse responses, so a field added on one side but missing on
the other becomes an import-time type error.

The package has **zero engine imports** — it is importable with only pydantic
present, so a pure client never pulls in lancedb/onnxruntime.
"""

from __future__ import annotations

from quarry.api.capture_ingest import CaptureIngestRequest
from quarry.api.captures import (
    CapturesLookupRequest,
    CapturesLookupResponse,
    CapturesPushResponse,
)
from quarry.api.collections import (
    CollectionInfo,
    CollectionList,
    DeleteCollectionRequest,
)
from quarry.api.databases import DatabaseInfo, DatabaseList
from quarry.api.deregister import DeregisterAccepted, DeregisterRequest
from quarry.api.documents import DeleteDocumentRequest, DocumentInfo, DocumentList
from quarry.api.errors import ErrorBody
from quarry.api.ingestion import IngestRequest, LearnRequest, RememberRequest
from quarry.api.maintenance import BackfillRequest, OptimizeRequest
from quarry.api.meta import CoverageResponse, HealthResponse, StatusResponse
from quarry.api.registrations import (
    RegisterRequest,
    RegistrationInfo,
    RegistrationList,
    RetainedCollection,
)
from quarry.api.search import SearchHit, SearchRequest, SearchResponse
from quarry.api.show import ShowPageResponse, ShowRequest
from quarry.api.tasks import TaskAccepted, TaskStatus

# The wire-protocol major version: the source of the ``/v{N}`` URL prefix on
# every engine route and the ``api_version`` a client negotiates against.
API_VERSION = "1"

__all__ = [
    "API_VERSION",
    "BackfillRequest",
    "CaptureIngestRequest",
    "CapturesLookupRequest",
    "CapturesLookupResponse",
    "CapturesPushResponse",
    "CollectionInfo",
    "CollectionList",
    "CoverageResponse",
    "DatabaseInfo",
    "DatabaseList",
    "DeleteCollectionRequest",
    "DeleteDocumentRequest",
    "DeregisterAccepted",
    "DeregisterRequest",
    "DocumentInfo",
    "DocumentList",
    "ErrorBody",
    "HealthResponse",
    "IngestRequest",
    "LearnRequest",
    "OptimizeRequest",
    "RegisterRequest",
    "RegistrationInfo",
    "RegistrationList",
    "RememberRequest",
    "RetainedCollection",
    "SearchHit",
    "SearchRequest",
    "SearchResponse",
    "ShowPageResponse",
    "ShowRequest",
    "StatusResponse",
    "TaskAccepted",
    "TaskStatus",
]
