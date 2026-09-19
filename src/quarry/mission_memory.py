"""Loop 2: file each frozen mission round as a memory in ``memory-<worker>``.

The client reads the sidecar files and POSTs content; the daemon never reads a
path. Idempotency comes from the document name plus an identity check on the
stored first line, so a re-run files nothing twice and never overwrites a
round another checkout filed under the same name.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self, final

from quarry.api import RememberRequest, ShowRequest
from quarry.client import HttpError, QuarryError
from quarry.memory_types import MemoryType
from quarry.mission_store import MissionStore
from quarry.mission_sync_types import MissionSyncOutcome, SyncOptions, SyncTally

if TYPE_CHECKING:
    from pathlib import Path

    from quarry.client import QuarryClient
    from quarry.mission_records import MissionContract, MissionRound
    from quarry.mission_store import MissionScan

logger = logging.getLogger(__name__)

_NOT_FOUND = 404
_SUMMARY_SIGNAL_CHARS = 100


@final
class MissionMemoryComposer:
    """Turn one frozen round into the ``remember`` body that records it.

    Stateless: every method derives from the contract and the round, so the
    CLI and the MCP tool cannot produce different documents for one round.
    """

    __slots__ = ()

    @staticmethod
    def document_name(contract: MissionContract, round_: MissionRound) -> str:
        """Return ``mission-<repo>-<id>-r<n>`` — unique per round across machines."""
        return f"mission-{contract.repo_name}-{contract.mission_id}-r{round_.number}"

    @staticmethod
    def collection(contract: MissionContract) -> str:
        """Return ``memory-<worker>`` — where the round is filed and looked up.

        Named explicitly on both the write and the existence check so the two
        cannot disagree: a show has no handle routing, and an unscoped show
        would report a same-named document in any collection as "filed".
        """
        return f"memory-{contract.worker}"

    @staticmethod
    def header(contract: MissionContract, round_: MissionRound) -> str:
        """Return the first line — the identity key the existence check compares."""
        return (
            f"# Mission {contract.mission_id} — round {round_.number} "
            f"(repo {contract.repo_name}, worker {contract.worker}, "
            f"evaluator {contract.evaluator}, created {contract.created_at})"
        )

    @classmethod
    def compose(
        cls, contract: MissionContract, round_: MissionRound
    ) -> RememberRequest:
        """Return the request, addressed to the worker's own memory collection."""
        return RememberRequest(
            name=cls.document_name(contract, round_),
            content=cls.document(contract, round_),
            collection=cls.collection(contract),
            format_hint="markdown",
            overwrite=True,
            agent_handle=contract.worker,
            memory_type=MemoryType.OBSERVATION.value,
            summary=cls.summary(contract, round_),
        )

    @classmethod
    def summary(cls, contract: MissionContract, round_: MissionRound) -> str:
        """Return ``<id> r<n> (<repo>): worker <verdict>/<recommendation> — <signal>``.

        The signal is the reflection's first, cut to 100 characters.
        """
        verdict = round_.result.verdict if round_.result else "none"
        recommendation = cls._recommendation(contract, round_)
        line = (
            f"{contract.mission_id} r{round_.number} ({contract.repo_name}): "
            f"worker {verdict}/{recommendation}"
        )
        if round_.reflection and round_.reflection.signals:
            line += f" — {round_.reflection.signals[0][:_SUMMARY_SIGNAL_CHARS]}"
        return line

    @classmethod
    def document(cls, contract: MissionContract, round_: MissionRound) -> str:
        """Render the markdown body (Appendix B of the design)."""
        result, reflection = round_.result, round_.reflection
        verdict = (
            f"Worker verdict (self-assessed): {result.verdict}, "
            f"confidence {result.confidence:.2f}"
            if result
            else "Worker verdict: none — no result submitted"
        )
        sections = [cls.header(contract, round_), "", verdict]
        if reflection:
            sections.append(
                f"Evaluator reflection: {reflection.recommendation} "
                f"(converging: {str(reflection.converging).lower()}), "
                f"authored by {reflection.author}"
            )
            sections.extend(("", "## Reflection signals"))
            sections.extend(f"- {signal}" for signal in reflection.signals)
            sections.extend(
                (
                    "",
                    "## Recommendation",
                    f"{reflection.recommendation} — {reflection.reason}",
                )
            )
        else:
            sections.append(
                f"Evaluator reflection: none — closed {contract.status} "
                f"at round {round_.number}"
            )
        if result:
            if result.open_questions:
                sections.extend(("", "## Open questions (worker)"))
                sections.extend(f"- {q}" for q in result.open_questions)
            sections.extend(("", "## Worker report", result.prose))
        return "\n".join(sections) + "\n"

    @staticmethod
    def _recommendation(contract: MissionContract, round_: MissionRound) -> str:
        if round_.reflection:
            return round_.reflection.recommendation
        return contract.status


@final
class MissionMemorySync:
    """File every frozen round of a scan that the daemon does not already hold."""

    __slots__ = ("_client", "_options")

    _client: QuarryClient
    _options: SyncOptions

    def __new__(cls, client: QuarryClient, options: SyncOptions) -> Self:
        self = super().__new__(cls)
        self._client = client
        self._options = options
        return self

    @classmethod
    def for_repo(
        cls, cwd: Path, client: QuarryClient, options: SyncOptions
    ) -> MissionSyncOutcome:
        """Scan the checkout containing *cwd* and sync it.

        The one entry both the CLI verb and the MCP tool call, so the two
        surfaces cannot build different request sequences from the same files.
        A repo with no missions tree is an empty sync — unless one mission was
        asked for by id, which then cannot be found and must say so.
        """
        store = MissionStore.for_repo(cwd)
        if store is not None:
            return cls(client, options).run(store.scan(options.mission_id))
        tally = SyncTally()
        if options.mission_id:
            tally.record_error(
                f"mission {options.mission_id} not found: "
                f"no .punt-labs/ethos/missions/ above {cwd}"
            )
        return tally.outcome(dry_run=options.dry_run)

    @staticmethod
    def requests(scan: MissionScan) -> list[tuple[str, RememberRequest]]:
        """Return ``(header, request)`` for every frozen, non-empty round in order."""
        composed: list[tuple[str, RememberRequest]] = []
        for record in scan.missions:
            for round_ in record.frozen_rounds():
                if round_.is_empty:
                    logger.info(
                        "missions: %s round %d has neither result nor reflection; "
                        "skipped",
                        record.contract.mission_id,
                        round_.number,
                    )
                    continue
                header = MissionMemoryComposer.header(record.contract, round_)
                composed.append(
                    (header, MissionMemoryComposer.compose(record.contract, round_))
                )
        return composed

    def run(self, scan: MissionScan) -> MissionSyncOutcome:
        """File, skip, or record an error for every composed round.

        Every daemon failure — a non-404 on the existence check, a 503 on the
        remember, an unreachable daemon — is one error line for that round,
        and the run continues: the rounds already filed stay in the tally, and
        the CLI's exit 1 on errors reports the ones that did not.
        """
        tally = SyncTally(scan.errors)
        for header, request in self.requests(scan):
            try:
                self._sync_round(header, request, tally)
            except QuarryError as exc:
                tally.record_error(f"{request.name}: {self._describe(exc)}")
        return tally.outcome(dry_run=self._options.dry_run)

    def _sync_round(
        self, header: str, request: RememberRequest, tally: SyncTally
    ) -> None:
        """Record one round's disposition; a daemon failure raises to :meth:`run`.

        The check-then-write is deliberately unlocked. Two syncs racing on one
        round both see 404 and both remember the same name into the same
        collection with byte-identical content; the daemon runs one FIFO
        writer per collection (``daemon.ingest_queue``), so the second
        overwrite replaces the first's chunks with identical ones — one chunk
        set survives, never two. The collision branch below guards a
        *different* round under the same name, which no ordering of identical
        writes can produce.
        """
        existing = self._existing_header(request.name, request.collection)
        if existing is not None and existing != header:
            tally.record_error(
                f"name collision: {request.name} holds a different round "
                f"({existing!r}) from another checkout; not overwritten"
            )
        elif existing is not None and not self._options.force:
            tally.record_skipped(request.name)
        else:
            if not self._options.dry_run:
                self._client.remember(request)
            tally.record_filed(request.name)

    @staticmethod
    def _describe(exc: QuarryError) -> str:
        """Return the error line's cause, with the wire status when there is one."""
        if exc.status:
            return f"daemon returned HTTP {exc.status}: {exc.message}"
        return exc.message

    def _existing_header(
        self, name: str, collection: str
    ) -> str | None:  # None: not filed yet (404)
        """Return the stored document's first line, or ``None`` when not filed yet.

        The lookup is scoped to *collection* — the same one the remember names —
        so only this worker's memory of the round counts as filed. ``None`` is
        the documented 404 outcome ("file it"); any other failure propagates to
        :meth:`run`, which records it rather than skipping.
        """
        try:
            page = self._client.show_page(
                ShowRequest(document=name, collection=collection, page=1)
            )
        except HttpError as exc:
            if exc.status == _NOT_FOUND:
                return None
            raise
        first = page.text.lstrip().splitlines()
        return first[0].strip() if first else ""
