"""
Protocol contract for the WebRTC Event Platform.

This package defines every WebSocket message type, error code, and validation
rule that the client–server protocol uses.  Future backend and frontend code
MUST import constants and validators from here rather than hard-coding strings.

Quick start
-----------
>>> from apps.protocol.validators import validate_message
>>> from apps.protocol.schemas import build_message
>>> from apps.protocol.constants import MessageType, ErrorCode, PROTOCOL_VERSION
"""
