"""Pin BLAS/OMP thread pools to one before the first numeric import.

Imported first by ``tools.eval.__init__`` so the single-thread environment is
in place *before* numpy/onnxruntime/quarry load and size their thread pools.
OpenBLAS reads ``OPENBLAS_NUM_THREADS`` at dlopen (which ``import numpy``
triggers); setting it afterward is a silent no-op. Under ``python -m tools.eval``
the package ``__init__`` runs before ``__main__``, so this module — imported as
``__init__``'s very first statement — is the true process entry for the pin.

Determinism of the eval run itself comes from the ORT arena, exact search, and
the docid tie-break, not from these pins; they remove the last float-reduction
reordering source as belt-and-suspenders (DES-032). This module is a bootstrap:
it runs ``ThreadPins.pin()`` at import, which is the whole reason it exists.
"""

from __future__ import annotations

import os


class ThreadPins:
    """The single-thread BLAS/OMP environment the eval process runs under."""

    __slots__ = ()

    # Every pool that can reorder a float reduction. Forced (not setdefault) so
    # the pin wins over any ambient cap; ThreadConfig later reads OMP via
    # setdefault, so this value survives.
    _VARS = (
        "OMP_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
        "VECLIB_MAXIMUM_THREADS",
    )

    @classmethod
    def pin(cls) -> None:
        """Force single-threaded BLAS/OMP and disable tokenizer parallelism."""
        for var in cls._VARS:
            os.environ[var] = "1"
        os.environ["TOKENIZERS_PARALLELISM"] = "false"


ThreadPins.pin()
