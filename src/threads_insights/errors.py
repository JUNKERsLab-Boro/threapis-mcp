"""Error types. Messages never contain tokens or request URLs."""


class ThreadsError(Exception):
    """Base error; str(e) is safe to show to MCP clients."""


class ConfigError(ThreadsError):
    pass


class ValidationError(ThreadsError):
    pass


class AuthError(ThreadsError):
    """Missing, invalid or expired access token (re-authentication needed)."""


class PermissionError_(ThreadsError):
    """Token valid but lacks permission (e.g. threads_manage_insights)."""


class RateLimitError(ThreadsError):
    pass


class NetworkError(ThreadsError):
    pass


class APIError(ThreadsError):
    pass


class AmbiguousOutcomeError(ThreadsError):
    """A write request may or may not have taken effect (timeout, 5xx, unparseable reply)."""
