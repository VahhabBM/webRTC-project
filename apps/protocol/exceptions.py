"""Protocol-specific exceptions."""

from __future__ import annotations

from .constants import ErrorCode


class ProtocolError(Exception):
    """Raised by validators when a message violates the protocol contract.

    The server MUST catch this exception and send a ``server.error`` message
    to the client rather than propagating it and crashing the connection.

    Attributes
    ----------
    code:
        One of the :class:`~apps.protocol.constants.ErrorCode` values.
    message:
        A human-readable description (suitable for ``server.error.message``).
    original_type:
        The ``"type"`` field from the offending message, if it could be
        extracted before the error was detected.
    detail:
        Optional structured context (will appear in ``server.error.detail``).
    """

    def __init__(
        self,
        code: ErrorCode | str,
        message: str,
        *,
        original_type: str | None = None,
        detail: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.code = str(code)
        self.message = message
        self.original_type = original_type
        self.detail = detail or {}

    def __repr__(self) -> str:
        return (
            f"ProtocolError(code={self.code!r}, message={self.message!r}, "
            f"original_type={self.original_type!r})"
        )
