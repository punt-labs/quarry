"""Human-readable text rendering of CLI result payloads.

Each formatter takes the same JSON-serializable structure the CLI emits in
``--json`` mode, so text and JSON output stay in lockstep across surfaces.
"""

from __future__ import annotations

from typing import final


@final
class ResultFormatter:
    """Render CLI result payloads as multi-line human-readable text."""

    __slots__ = ()

    @staticmethod
    def sync_results(json_data: dict[str, dict[str, object]]) -> str:
        """Format a ``{collection: result}`` mapping as a multi-line summary."""
        lines: list[str] = []
        for col, res in json_data.items():
            line = (
                f"{col}: {res.get('ingested', 0)} ingested, "
                f"{res.get('refreshed', 0)} refreshed, "
                f"{res.get('deleted', 0)} deleted, "
                f"{res.get('skipped', 0)} unchanged, {res.get('failed', 0)} failed"
            )
            errors = res.get("errors")
            if isinstance(errors, list) and errors:
                line += "\n" + "\n".join(f"  error: {e}" for e in errors)
            lines.append(line)
        return "\n".join(lines)

    @staticmethod
    def registrations(regs: list[dict[str, object]]) -> str:
        """Format registered directories as ``collection: directory`` lines."""
        if not regs:
            return "No registered directories."
        return "\n".join(
            f"{reg.get('collection', '')}: {reg.get('directory', '')}" for reg in regs
        )

    @staticmethod
    def databases(databases: list[dict[str, object]]) -> str:
        """Format named databases with document counts and storage size."""
        if not databases:
            return "No databases found."
        return "\n".join(
            f"{db.get('name', '')}: {db.get('document_count', 0)} documents, "
            f"{db.get('size_description', '')}"
            for db in databases
        )

    @staticmethod
    def coerce_results(data: object) -> dict[str, dict[str, object]]:
        """Coerce a remote JSON payload to the ``{collection: result}`` shape."""
        if not isinstance(data, dict):
            return {}
        return {str(k): v if isinstance(v, dict) else {} for k, v in data.items()}

    @staticmethod
    def has_failures(data: dict[str, dict[str, object]]) -> bool:
        """Return whether any project failed to push (aborted or push error)."""
        return any(not res.get("pushed") for res in data.values())

    @staticmethod
    def captures_push(data: dict[str, dict[str, object]]) -> str:
        """Format per-project shadow-push results as a multi-line summary."""
        if not data:
            return "No shadow-enabled projects to push."
        lines: list[str] = []
        for col, res in data.items():
            rescrubbed = res.get("rescrubbed", 0)
            reason = res.get("aborted_reason")
            if reason:
                lines.append(f"{col}: not pushed ({reason}); rescrubbed {rescrubbed}")
            elif res.get("pushed"):
                lines.append(f"{col}: pushed; rescrubbed {rescrubbed}")
            else:
                lines.append(f"{col}: committed, push failed; rescrubbed {rescrubbed}")
        return "\n".join(lines)
