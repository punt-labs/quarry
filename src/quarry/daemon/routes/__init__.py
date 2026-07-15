"""Daemon REST route groups — one class per resource, bound to a context."""

from __future__ import annotations

from quarry.daemon.routes.captures import CaptureRoutes
from quarry.daemon.routes.collections import CollectionRoutes
from quarry.daemon.routes.databases import DatabaseRoutes
from quarry.daemon.routes.documents import DocumentRoutes
from quarry.daemon.routes.ingestion import IngestionRoutes
from quarry.daemon.routes.meta import MetaRoutes
from quarry.daemon.routes.registrations import RegistrationRoutes
from quarry.daemon.routes.search import SearchRoutes
from quarry.daemon.routes.show import ShowRoutes
from quarry.daemon.routes.sync import SyncRoutes
from quarry.daemon.routes.task_status import TaskStatusRoutes

__all__ = [
    "CaptureRoutes",
    "CollectionRoutes",
    "DatabaseRoutes",
    "DocumentRoutes",
    "IngestionRoutes",
    "MetaRoutes",
    "RegistrationRoutes",
    "SearchRoutes",
    "ShowRoutes",
    "SyncRoutes",
    "TaskStatusRoutes",
]
