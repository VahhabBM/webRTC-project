"""T-44 message-layer load-test tool (staging/local only).

This package is not imported by the WebSocket consumer, orchestrator, or
any production request path. It reuses the existing T-08/T-14/T-24 contracts.
"""

__all__ = ["DEFAULT_CLIENT_COUNT", "MAX_CLIENT_COUNT", "MIN_CLIENT_COUNT"]

DEFAULT_CLIENT_COUNT = 50
MIN_CLIENT_COUNT = 4
MAX_CLIENT_COUNT = 200
