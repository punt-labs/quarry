"""The shared stand-in for the ONNX embedding backend.

One fake, installed at the factory by an autouse fixture, replaces the roughly
forty independent ``patch("...get_embedding_backend", ...)`` context managers
that each named a single import site.  Any test reaching the model through a
site none of those patches happened to name would silently load 410 MB and
still pass; the fixture closes that by construction, and this class is what it
installs.
"""

from __future__ import annotations

import ipaddress
import zlib
from contextlib import contextmanager
from typing import TYPE_CHECKING, Final, Self, final
from unittest.mock import patch

import numpy as np

if TYPE_CHECKING:
    from collections.abc import Generator

    from numpy.typing import NDArray

    from quarry.backend_protocols import EmbeddingBackend
    from quarry.config import Settings

DEFAULT_DIMENSION: Final[int] = 768

# A DNS label is at most 63 characters; IDNA encoding raises past that.
_MAX_DNS_LABEL: Final[int] = 63


@final
class FakeEmbeddingBackend:
    """Deterministic embeddings derived from the text, with no ONNX session.

    Vectors are seeded per text rather than fixed, for two reasons.  Constant
    vectors are degenerate under the cosine metric LanceDB indexes with, and
    identical vectors for distinct documents make any ranking assertion
    meaningless.  Seeding from a checksum of the text keeps the mapping stable
    across processes and runs -- ``hash()`` would not, being salted per process.

    Vectors are unit-length, matching the real backend, so any code that assumes
    cosine similarity equals the dot product behaves the same against both.

    ``embedded`` records every text that passed through, queries included, so a
    test can assert on what was embedded without a second spy.

    Satisfies :class:`quarry.backend_protocols.EmbeddingBackend` structurally.
    """

    __slots__ = ("_dimension", "_embedded", "_model_name")

    _dimension: int
    _model_name: str
    _embedded: list[str]

    def __new__(
        cls,
        dimension: int = DEFAULT_DIMENSION,
        *,
        model_name: str = "fake-embedder",
    ) -> Self:
        self = super().__new__(cls)
        self._dimension = dimension
        self._model_name = model_name
        self._embedded = []
        return self

    @classmethod
    def for_settings(cls, settings: Settings) -> Self:
        """Return a fake shaped to *settings*, standing in for the cached factory."""
        return cls(settings.embedding_dimension)

    @property
    def dimension(self) -> int:
        """Return the width of the vectors this backend produces."""
        return self._dimension

    @property
    def model_name(self) -> str:
        """Return the name a diagnostic would print for this backend."""
        return self._model_name

    @property
    def embedded(self) -> tuple[str, ...]:
        """Return every text this backend has embedded, in order.

        A snapshot, not the live list: handing out the internal one would let a
        caller mutate the record it is meant to be reading.
        """
        return tuple(self._embedded)

    def embed_texts(self, texts: list[str]) -> NDArray[np.float32]:
        """Return one vector per text, shape ``(len(texts), dimension)``."""
        self._embedded.extend(texts)
        if not texts:
            return np.zeros((0, self._dimension), dtype=np.float32)
        return np.vstack([self._vector(text) for text in texts])

    def embed_query(self, query: str) -> NDArray[np.float32]:
        """Return a single vector of shape ``(dimension,)``."""
        self._embedded.append(query)
        return self._vector(query)

    def _vector(self, text: str) -> NDArray[np.float32]:
        """Return the unit-length vector this text always maps to.

        L2-normalized because the real backend is: quarry stores and queries on
        the understanding that cosine similarity equals the dot product, so a
        fake emitting unnormalized vectors would diverge from production on
        every norm-sensitive path while still looking plausible.
        """
        rng = np.random.default_rng(zlib.crc32(text.encode()))
        vector = rng.standard_normal(self._dimension).astype(np.float32)
        norm = float(np.linalg.norm(vector))
        unit: NDArray[np.float32] = (vector / (norm or 1.0)).astype(np.float32)
        return unit


@final
class FakeResolver:
    """Deterministic stand-in for ``socket.getaddrinfo`` on quarry's fetch gate.

    Installed for every test by an autouse fixture, because a suite that reaches
    real DNS is not hermetic: it fails when the network blips and it makes an
    outbound request per fetch-gate check. The default answer is a public
    address, so a host passes the SSRF policy exactly as a real public host
    would; a test asserting on the policy's *rejections* patches this seam
    itself with the address it wants classified.

    ``getaddrinfo``'s real signature takes host, port, and four optional
    arguments, and quarry calls it as ``getaddrinfo(host, None)``. Accepting
    anything keeps the fake usable wherever it is installed.
    """

    __slots__ = ("_address", "_resolved")

    _address: str
    _resolved: list[str]

    def __new__(cls, address: str = "93.184.216.34") -> Self:
        self = super().__new__(cls)
        self._address = address
        self._resolved = []
        return self

    @property
    def resolved(self) -> tuple[str, ...]:
        """Return every host this resolver was asked about, in order."""
        return tuple(self._resolved)

    def __call__(
        self, host: str, *_args: object, **_kwargs: object
    ) -> list[tuple[object, object, object, str, tuple[str, int]]]:
        """Return one address for *host*, in ``getaddrinfo``'s 5-tuple shape.

        An address literal resolves to itself, as it does for real. Answering
        the canned public address for ``10.0.0.1`` would tell the SSRF policy
        that a private address is public and quietly disarm it.
        """
        self._resolved.append(host)
        self._reject_overlong_label(host)
        return [(None, None, None, "", (self._literal_or_default(host), 0))]

    @staticmethod
    def _reject_overlong_label(host: str) -> None:
        """Raise ``UnicodeError`` on an over-long label, as the real resolver does.

        A DNS label is at most 63 characters, and IDNA encoding raises
        ``UnicodeError`` -- a ``ValueError``, notably not an ``OSError`` -- past
        that. quarry's resolution boundary catches both to fail closed, so a
        fake that always succeeded would leave that boundary untested.
        """
        if any(len(label) > _MAX_DNS_LABEL for label in host.split(".")):
            msg = f"label empty or too long: {host!r}"
            raise UnicodeError(msg)

    def _literal_or_default(self, host: str) -> str:
        """Return *host* when it is already an address, else the canned one."""
        try:
            return str(ipaddress.ip_address(host.strip("[]")))
        except ValueError:
            return self._address


@contextmanager
def patched_embedder(embedder: EmbeddingBackend) -> Generator[EmbeddingBackend]:
    """Pin *embedder* as the streaming path's backend for the block.

    The autouse fixture installs a FRESH fake per factory call, which is right
    for isolation and useless for counting: a test that wants to assert on what
    was embedded needs one instance it holds a reference to.  This pins that
    instance at the one site the ingest path reads.
    """
    with patch(
        "quarry.ingestion.streaming.get_embedding_backend", return_value=embedder
    ):
        yield embedder
