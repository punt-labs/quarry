"""The doctor check that the Tantivy FTS index is queryable."""

from __future__ import annotations

from typing import TYPE_CHECKING, final

from quarry.doctor_failure import CheckFailure
from quarry.results import CheckResult

if TYPE_CHECKING:
    from pathlib import Path


@final
class FtsIndexCheck:
    """Probe the chunks table's full-text index; every outcome is advisory."""

    __slots__ = ()

    @classmethod
    def check(cls, db_path: Path) -> CheckResult:
        """Verify the Tantivy FTS index is queryable."""
        # LanceDB and pyarrow are native: a broken install is a failed check, not an
        # import error that takes the whole report down.
        try:
            from quarry.db.facade import Database  # noqa: PLC0415
            from quarry.db.schema import TABLE_NAME  # noqa: PLC0415
        except CheckFailure.BROKEN_STACK as exc:
            return cls._result(
                passed=False, message=CheckFailure.import_message("quarry.db", exc)
            )

        if not db_path.exists():
            return cls._result(passed=True, message="no database yet")
        try:
            database = Database.connect(db_path)
            if TABLE_NAME not in database.db.list_tables().tables:
                return cls._result(passed=True, message="no table yet")
            table = database.db.open_table(TABLE_NAME)
            table.search("health", query_type="fts").limit(1).to_list()
            return cls._result(passed=True, message="healthy")
        except (OSError, ValueError, RuntimeError) as exc:
            return cls._result(passed=False, message=CheckFailure.index_message(exc))
        except Exception as exc:  # noqa: BLE001
            return cls._result(
                passed=False, message=f"error: {CheckFailure.detail(exc)}"
            )

    @staticmethod
    def _result(*, passed: bool, message: str) -> CheckResult:
        """Return one ``FTS index`` report line."""
        return CheckResult(
            name="FTS index", passed=passed, message=message, required=False
        )
