"""Quarry's daemon wire contract — Pydantic request/response/error models.

One request and one response model per REST operation the daemon serves.
This package is the single source of truth for the wire shape: the daemon
imports it to type and validate its handlers, and ``QuarryClient`` will import
the same models to build requests and parse responses, so a field added on one
side but missing on the other becomes an import-time type error.

The package has **zero engine imports** — it is importable with only pydantic
present, so a pure client never pulls in lancedb/onnxruntime.
"""

from __future__ import annotations

from quarry.api.captures import CapturesPushResponse
from quarry.api.collections import (
    CollectionInfo,
    CollectionList,
    DeleteCollectionRequest,
)
from quarry.api.databases import DatabaseInfo, DatabaseList
from quarry.api.deregister import DeregisterAccepted, DeregisterRequest
from quarry.api.documents import DeleteDocumentRequest, DocumentInfo, DocumentList
from quarry.api.errors import ErrorBody
from quarry.api.ingestion import IngestRequest, RememberRequest
from quarry.api.maintenance import BackfillRequest, OptimizeRequest
from quarry.api.meta import HealthResponse, StatusResponse
from quarry.api.registrations import (
    RegisterRequest,
    RegistrationInfo,
    RegistrationList,
)
from quarry.api.search import SearchHit, SearchRequest, SearchResponse
from quarry.api.show import ShowPageResponse, ShowRequest
from quarry.api.tasks import TaskAccepted, TaskStatus

# The wire-protocol major version — reserved for a future versioned URL space
# (``/v1/…``) and ``/health`` field; not yet on the wire, today's routes are bare.
API_VERSION = "1"

__all__ = [
    "API_VERSION",
    "BackfillRequest",
    "CapturesPushResponse",
    "CollectionInfo",
    "CollectionList",
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
    "OptimizeRequest",
    "RegisterRequest",
    "RegistrationInfo",
    "RegistrationList",
    "RememberRequest",
    "SearchHit",
    "SearchRequest",
    "SearchResponse",
    "ShowPageResponse",
    "ShowRequest",
    "StatusResponse",
    "TaskAccepted",
    "TaskStatus",
]
