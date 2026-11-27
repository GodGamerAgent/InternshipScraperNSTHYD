"""Decide whether a job posting is an internship (rules from ``config/sources.yaml``)."""

import re
from collections.abc import Sequence
from typing import Protocol, Self


class InternshipRules(Protocol):
    """Shape of ``sources.yaml: internship`` (e.g. ``collectors.config.InternshipConfig``)."""

    include_patterns: Sequence[str]
    exclude_title_patterns: Sequence[str]
    employment_type_patterns: Sequence[str]


class InternshipMatcher:
    """Title-first internship classifier.

    A posting is an internship when its title matches an ``include_patterns`` regex **or** its ATS
    employment-type field matches ``employment_type_patterns`` — and the title matches none of the
    ``exclude_title_patterns`` (seniority / contract markers). Descriptions are never consulted:
    full-time JDs routinely mention interns ("you will mentor interns").
    """

    def __init__(
        self,
        include: Sequence[str],
        exclude: Sequence[str] | None = None,
        employment_types: Sequence[str] | None = None,
    ) -> None:
        def compile_all(patterns: Sequence[str] | None) -> list[re.Pattern[str]]:
            return [re.compile(p, re.IGNORECASE) for p in patterns or []]

        self._include = compile_all(include)
        self._exclude = compile_all(exclude)
        self._employment = compile_all(employment_types)

    @classmethod
    def from_config(cls, config: InternshipRules) -> Self:
        return cls(
            config.include_patterns,
            config.exclude_title_patterns,
            config.employment_type_patterns,
        )

    def is_internship(self, title: str, *, employment_type: str | None = None) -> bool:
        """Classify a posting from its title and optional employment-type/commitment text."""
        if any(p.search(title) for p in self._exclude):
            return False
        if any(p.search(title) for p in self._include):
            return True
        return bool(employment_type) and any(
            p.search(employment_type or "") for p in self._employment
        )
