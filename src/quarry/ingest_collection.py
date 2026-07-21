"""The LanceDB collection a URL ingest writes — the daemon queue's routing key."""

from __future__ import annotations

from typing import Self, final
from urllib.parse import urlparse


@final
class IngestCollection:
    """The collection a URL ingest targets: an explicit name, else the URL host.

    A plain ``quarry ingest`` may omit ``--collection``; the pipeline then
    derives the URL hostname (``default`` when the URL has none).  The daemon's
    serialized queue keys a job on this SAME resolved name, so an explicit
    ``collection=H`` request and an omitted-collection request for host ``H``
    route to one FIFO worker — the single-writer-per-table guarantee the queue
    exists to hold (DES-042).  Route and pipeline share this one resolver so the
    key the queue serializes on can never drift from the table the job writes.
    """

    _FALLBACK_HOST = "default"

    _name: str

    def __new__(cls, name: str) -> Self:
        self = super().__new__(cls)
        self._name = name
        return self

    @property
    def name(self) -> str:
        """Return the resolved collection name (the queue's routing key)."""
        return self._name

    @classmethod
    def resolve(cls, url: str, collection: str) -> Self:
        """Return *collection* if given, else the URL hostname (or the fallback).

        An empty *collection* is the "let the pipeline decide" signal a plain
        ingest sends; it resolves to ``urlparse(url).hostname`` or, for a URL
        with no host, ``default`` — never left empty, so the queue key is always
        the concrete table the job writes.
        """
        if collection:
            return cls(collection)
        return cls(urlparse(url).hostname or cls._FALLBACK_HOST)
