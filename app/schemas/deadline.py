"""Wire dates are RFC3339 strings with a known UTC offset; null is not a wire date."""

import re
from datetime import datetime
from typing import Annotated

from pydantic import BeforeValidator, Field, WithJsonSchema

UTC_RFC3339_PATTERN = r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|\+00:00)$"


def parse_deadline(value: object) -> datetime:
    if not isinstance(value, str) or not re.fullmatch(UTC_RFC3339_PATTERN, value):
        raise ValueError("deadline_at must be an RFC3339 UTC string")
    return datetime.fromisoformat(value.upper().replace("Z", "+00:00"))


def omit_internal_default(schema: dict) -> None:
    schema.pop("default", None)


# None is the internal default for omission. An explicit wire null is rejected.
DeadlineUTC = Annotated[
    datetime | None,
    BeforeValidator(parse_deadline),
    WithJsonSchema({"type": "string", "format": "date-time", "pattern": UTC_RFC3339_PATTERN}),
    Field(json_schema_extra=omit_internal_default),
]
