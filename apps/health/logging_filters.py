import logging
import re

SENSITIVE_PATTERNS = [
    re.compile(r"token=[\w\-\.]+", re.IGNORECASE),
    re.compile(r"(/join/)[^/\s]+", re.IGNORECASE),
    re.compile(r"password=\S+", re.IGNORECASE),
    re.compile(r"[\w\.-]+@[\w\.-]+\.\w+"),  # ایمیل
]


class RedactSensitiveDataFilter(logging.Filter):
    def filter(self, record):
        message = record.getMessage()
        for pattern in SENSITIVE_PATTERNS:
            message = pattern.sub("[REDACTED]", message)
        record.msg = message
        record.args = ()
        if not hasattr(record, "request_id"):
            record.request_id = "-"
        return True
