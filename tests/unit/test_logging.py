"""ADR-019: "a test asserting that a request carrying a CPF produces no log line
containing it." The request pipeline that would carry a CPF arrives in M1; this is the
part of the guarantee that already exists — the formatter redacts by field name and cannot
be bypassed by a call site forgetting to scrub a value.
"""

import json
import logging

from cineatelie.core.logging import JsonFormatter


def _format(**extra: object) -> dict[str, object]:
    record = logging.LogRecord(
        name="cineatelie.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="client saved",
        args=(),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return json.loads(JsonFormatter().format(record))


def test_cpf_value_never_appears_in_the_output() -> None:
    payload = _format(cpf="123.456.789-00")
    rendered = json.dumps(payload)
    assert "123.456.789-00" not in rendered
    assert payload["cpf"] == "[REDACTED]"


def test_redaction_is_case_insensitive_on_the_field_name() -> None:
    payload = _format(CPF="123.456.789-00")
    assert payload["CPF"] == "[REDACTED]"


def test_measurements_are_redacted() -> None:
    payload = _format(measurements={"busto": 90.0})
    assert payload["measurements"] == "[REDACTED]"


def test_ordinary_fields_pass_through() -> None:
    payload = _format(tenant_id="abc-123", route="/clients")
    assert payload["tenant_id"] == "abc-123"
    assert payload["route"] == "/clients"


def test_message_and_level_are_present() -> None:
    payload = _format()
    assert payload["message"] == "client saved"
    assert payload["level"] == "INFO"
