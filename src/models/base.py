"""Shared base class and reusable annotated field types for all domain models."""

import re
from datetime import UTC, datetime
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field

from src.core.urls import is_http_url
from src.models.enums import CITY_UNKNOWN

_SLUG = re.compile(r"^[a-z][a-z0-9_]*$")


def utc_now() -> datetime:
    """Current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def _ensure_utc(value: datetime) -> datetime:
    # SQLite round-trips drop tzinfo; we only ever store UTC, so naive means UTC.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _city_slug(value: Any) -> Any:
    if value is None or (isinstance(value, str) and not value.strip()):
        return CITY_UNKNOWN
    return value.strip().lower() if isinstance(value, str) else value


def _check_slug(value: str) -> str:
    if not _SLUG.match(value):
        raise ValueError(f"must be a lowercase slug like 'delhi_ncr', got {value!r}")
    return value


def _check_http_url(value: str) -> str:
    if not is_http_url(value):
        raise ValueError(f"must be an absolute http(s) URL, got {value!r}")
    return value


def _clean_tags(value: Any) -> Any:
    if value is None:
        return []
    if isinstance(value, list | tuple | set):
        seen: dict[str, None] = {}
        for item in value:
            tag = str(item).strip().lower()
            if tag:
                seen.setdefault(tag, None)
        return list(seen)
    return value


UtcDatetime = Annotated[datetime, AfterValidator(_ensure_utc)]
"""Datetime normalized to aware UTC (naive input is assumed to already be UTC)."""

Slug = Annotated[str, AfterValidator(_check_slug)]
"""Lowercase ``snake_case`` identifier (source names, city slugs)."""

CitySlug = Annotated[str, BeforeValidator(_city_slug), AfterValidator(_check_slug)]
"""Canonical city slug; ``None``/blank becomes ``"unknown"``."""

HttpUrlStr = Annotated[str, AfterValidator(_check_http_url)]
"""Absolute http(s) URL kept as a plain string (cheap to store and compare)."""

Tags = Annotated[list[str], BeforeValidator(_clean_tags)]
"""Lowercased, de-duplicated, order-preserving tag list (``None`` -> ``[]``)."""

Latitude = Annotated[float, Field(ge=-90, le=90)]
Longitude = Annotated[float, Field(ge=-180, le=180)]


class DomainModel(BaseModel):
    """Strict base for every data contract: unknown fields are errors, strings are stripped."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
    )
