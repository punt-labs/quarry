"""The Quarry daemon — the engine-side process serving the REST API.

Under DES-031 v2.2 the engine lives only in the daemon process; client
surfaces reach it over the wire.  This package holds the daemon's own pieces
(request guards, route groups, the app factory) as they are decomposed out of
the historical ``http_server`` god module.
"""

from __future__ import annotations

__all__: list[str] = []
