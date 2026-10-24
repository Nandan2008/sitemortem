"""Small shared helpers: secret redaction and user-facing errors."""
from __future__ import annotations

import os
import re

_SECRET_ENV = ("NEBIUS_API_KEY", "TAVILY_API_KEY")
_PATTERNS = [re.compile(r"tvly-[A-Za-z0-9_\-]{6,}"), re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{8,}"),
             re.compile(r"(?i)(api[_-]?key[\"'=:\s]+)[A-Za-z0-9._\-]{12,}")]


def redact(text: object) -> str:
    """Remove API keys from any text before it is logged or shown."""
    s = str(text)
    for name in _SECRET_ENV:
        v = os.getenv(name)
        if v and len(v) >= 6:
            s = s.replace(v, "[redacted]")
    for p in _PATTERNS:
        s = p.sub(lambda m: (m.group(1) if m.groups() else "") + "[redacted]", s)
    return s


class UserError(Exception):
    """An error whose message is safe and useful to show in the dashboard."""
