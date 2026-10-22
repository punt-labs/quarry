"""The base type of an expected refusal whose message is safe to show a client."""

from __future__ import annotations


class RefusalError(ValueError):
    """Raised for an expected "no": the message is a stable reason, safe to show.

    A refusal is an answer, not a fault.  Its message never embeds a URL, a
    credential or a caller-supplied name, so it may be logged at WARNING without
    a traceback and returned to a client verbatim.  Anything that is not a
    :class:`RefusalError`, a stray ``ValueError`` included, fails closed: the client
    sees only the exception's class.
    """


class RegistrationRefusedError(RefusalError):
    """Raised when a directory registration is refused: nested, duplicate or taken.

    The message names the directory or collection the caller asked about, which
    it already knows, and never a URL.
    """
