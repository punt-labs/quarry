"""Request-shape guards shared by the HTTP routes: body size and bool fields."""

from __future__ import annotations

from typing import TYPE_CHECKING, final

from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from starlette.requests import Request


@final
class RequestGuards:
    """Validate request bodies before a route touches the database."""

    __slots__ = ()

    @staticmethod
    def check_body_size(request: Request, limit: int) -> JSONResponse | None:
        """Reject requests whose advertised body size exceeds *limit*.

        Also rejects chunked-encoding requests with no ``Content-Length`` header
        so the server cannot be forced to stream arbitrary bytes before noticing.
        """
        header = request.headers.get("content-length")
        if header is None:
            return JSONResponse(
                {"error": "Content-Length header required"},
                status_code=411,
            )
        try:
            length = int(header)
        except ValueError:
            return JSONResponse(
                {"error": "Invalid Content-Length header"}, status_code=400
            )
        if length < 0:
            return JSONResponse(
                {"error": "Invalid Content-Length header"}, status_code=400
            )
        if length > limit:
            return JSONResponse(
                {"error": f"Request body too large (max {limit} bytes)"},
                status_code=413,
            )
        return None

    @staticmethod
    def coerce_bool_field(
        body: dict[str, object], field: str, *, default: bool
    ) -> bool | JSONResponse:
        """Return the bool value of ``body[field]`` or a 400 response.

        Rejects any non-bool non-null value.  Python's ``bool()`` coerces the
        strings ``"false"`` and ``"0"`` to ``True`` — use this helper instead to
        preserve caller intent.
        """
        value = body.get(field)
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        return JSONResponse(
            {"error": f"Field {field!r} must be a boolean"},
            status_code=400,
        )

    @staticmethod
    def coerce_int_field(
        body: dict[str, object], field: str, *, default: int
    ) -> int | JSONResponse:
        """Return the int value of ``body[field]`` or a 400 response.

        Rejects bools (``True``/``False`` are ``int`` subclasses) and any other
        non-int non-null value, mirroring :meth:`coerce_bool_field`.
        """
        value = body.get(field)
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, int):
            return JSONResponse(
                {"error": f"Field {field!r} must be an integer"},
                status_code=400,
            )
        return value
