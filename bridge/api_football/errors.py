"""Fixed-code, non-sensitive errors. Never embed provider bodies or credentials."""
from __future__ import annotations


class FootballError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class ProviderUnavailable(FootballError):
    """Bounded HTTP transport error, optionally retryable."""

    def __init__(self, code: str, *, retryable: bool = True):
        self.retryable = retryable
        super().__init__(code)


class ProviderHTTPError(FootballError):
    def __init__(self, status: int):
        self.status = status
        self.retryable = status in (500, 502, 503, 504)
        super().__init__(f"PROVIDER_HTTP_{status}")


class ProviderDataError(FootballError):
    """Response cannot be safely interpreted: no implicit defaults."""


class CircuitOpen(FootballError):
    def __init__(self):
        super().__init__("PROVIDER_CIRCUIT_OPEN")


class RequestQueueFull(FootballError):
    def __init__(self):
        super().__init__("PROVIDER_REQUEST_QUEUE_TIMEOUT")
