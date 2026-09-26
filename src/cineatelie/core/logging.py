"""Structured JSON logging with request-scoped context and field-name redaction (ADR-019).

Full request correlation (`request_id` bound by `RequestIdMiddleware`, `tenant_id`/`user_id`
bound by the auth/tenant middleware) lands in M1 with the rest of the middleware chain. What
is here now — the context vars, the JSON formatter, and the redaction filter — is the part
that has no dependency on there being a request at all, and is the part ADR-019 asks to be
provably correct on its own: "a request carrying a CPF produces no log line containing it."
That guarantee starts here, at the formatter, not at the call site — a call site can always
forget to scrub a value; a formatter that redacts by field name cannot be bypassed by
forgetting.
"""

from __future__ import annotations

import json
import logging
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

# Fields that must never reach a log line, whatever value they carry (ADR-013, ADR-019).
# Keep this list and the one in the docstring test below in sync in the same commit as any
# new personal-data field (implementation plan §4, "definition of done").
REDACTED_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "cpf",
        "rg",
        "cnpj",
        "pix_key",
        "password",
        "token",
        "access_token",
        "refresh_token",
        "authorization",
        "cookie",
        "measurements",
        "body_text",  # contract bodies can carry a client's name/address inline
    }
)

REDACTED_PLACEHOLDER = "[REDACTED]"

# Request-scoped context. Set by middleware in M1; read here so every log line carries them
# automatically without every call site threading them through by hand.
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
tenant_id_var: ContextVar[str | None] = ContextVar("tenant_id", default=None)
user_id_var: ContextVar[str | None] = ContextVar("user_id", default=None)
role_var: ContextVar[str | None] = ContextVar("role", default=None)

_STANDARD_RECORD_ATTRS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys())


def _redact(key: str, value: Any) -> Any:
    return REDACTED_PLACEHOLDER if key.lower() in REDACTED_FIELD_NAMES else value


class JsonFormatter(logging.Formatter):
    """One JSON object per line. Any `extra={...}` field is included, redacted by name."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        request_id = request_id_var.get()
        tenant_id = tenant_id_var.get()
        user_id = user_id_var.get()
        role = role_var.get()
        if request_id is not None:
            payload["request_id"] = request_id
        if tenant_id is not None:
            payload["tenant_id"] = tenant_id
        if user_id is not None:
            payload["user_id"] = user_id
        if role is not None:
            payload["role"] = role

        for key, value in record.__dict__.items():
            if key in _STANDARD_RECORD_ATTRS or key in payload:
                continue
            payload[key] = _redact(key, value)

        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    """Call once, at process start. Replaces the root handler with the JSON formatter."""
    root = logging.getLogger()
    root.setLevel(level.upper())
    root.handlers.clear()

    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
