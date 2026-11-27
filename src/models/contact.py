"""``Contact`` — a publicly listed recruiting/founder contact for a startup."""

import re
from typing import Self

from pydantic import Field, field_validator, model_validator

from src.core.urls import normalize_url
from src.models.base import DomainModel, HttpUrlStr, UtcDatetime, utc_now
from src.models.enums import ContactSourceMethod

# Pragmatic RFC 5322 subset: good enough to reject scraped junk without an extra dependency.
_EMAIL = re.compile(
    r"^[a-z0-9.!#$%&'*+/=?^_`{|}~-]+"  # local part
    r"@(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$"  # domain labels + TLD
)


class Contact(DomainModel):
    """A public contact. Needs at least one of ``email`` / ``profile_url``.

    Compliance guard: ``pattern_mx_verified`` contacts (guessed patterns) are only valid once the
    domain's MX record has been confirmed (``verified_mx=True``).
    """

    id: int | None = None
    startup_id: int
    full_name: str | None = Field(default=None, max_length=200)
    role: str | None = Field(default=None, max_length=200)
    email: str | None = None
    profile_url: HttpUrlStr | None = None
    source_method: ContactSourceMethod
    confidence_score: float = Field(ge=0.0, le=1.0)
    verified_mx: bool = False
    discovered_at: UtcDatetime = Field(default_factory=utc_now)

    @field_validator("email", mode="before")
    @classmethod
    def _email(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        email = value.strip().removeprefix("mailto:").split("?", 1)[0].lower()
        if not email:
            return None
        if not _EMAIL.match(email):
            raise ValueError(f"invalid email address: {value!r}")
        return email

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if not self.email and not self.profile_url:
            raise ValueError("a contact needs an email or a profile_url")
        if self.source_method is ContactSourceMethod.PATTERN_MX_VERIFIED and not self.verified_mx:
            raise ValueError("pattern_mx_verified contacts require verified_mx=True")
        return self

    @property
    def dedupe_key(self) -> str:
        """Per-startup natural key: ``email:<lower>`` if an email exists, else ``url:<profile>``."""
        if self.email:
            return f"email:{self.email}"
        assert self.profile_url is not None  # guaranteed by _consistent
        return f"url:{normalize_url(self.profile_url)}"
